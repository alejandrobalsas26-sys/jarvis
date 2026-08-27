"""scripts/project_m62_gen14_capacity.py — V69 M62 S3Y: does generation 14 still fit?

WHY THIS EXISTS
---------------
Generation 13 closed at 33 788 bytes against a 34 816-byte budget: **1 028 bytes of
headroom**, and the policy floor is 1 024. Four bytes of slack. Generation 14 has to carry
strictly more truth than that one — a spent holdout that names what spent it, a candidate
that stops being unmeasured, and a measured result — and it has to do it without raising a
reviewed budget, because raising one again is exactly what generation 13's own
``ruled_out`` bars.

So capacity is proved BEFORE a single model weight is loaded, and before any EVAL
authority is requested. Discovering after the holdout is spent that the repository cannot
record what it measured would be the one outcome an evaluation may not produce: eval-v6
is single-use, and a rerun to "fix" the recording is forbidden by the same rules.

The projection is not a guess. :func:`project_gen14` is a pure transform of the real
generation-13 snapshot, measured through the same ``canonical_bytes`` the verifier and the
emitter use, so the bytes counted here are the bytes that would land on disk.

THE FOUR TERMINAL STATES
------------------------
An evaluation has more than one truthful ending, and capacity has to be proved for the
LARGEST of them, not the most convenient:

``ELIGIBLE``
    Every gate passes. Candidate 004 becomes ``EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW`` —
    which is a request for a human decision, not a promotion.
``NOT_ELIGIBLE``
    One or more gates fail. Candidate 004 becomes ``EVALUATED_NOT_ELIGIBLE``.
``ABORTED``
    The runtime begins model-facing access and then stops before a normal completion.
    eval-v6 is STILL SPENT — the spend boundary is the durable
    ``holdout_model_facing_committed`` event, deliberately earlier than proof a forward
    pass finished (generation-13 invariant, limitation 16) — but no portable receipt
    exists, so candidate 004 may NOT claim an ``EVALUATED_*`` state. It stays
    ``TRAINED_UNEVALUATED`` beside a spent holdout, which is an ugly state and a true one.
``DURABILITY_FAILURE``
    A valid measurement is taken and its durable evidence does not land. The generation-13
    invariant is explicit that this is NOT a clean success and that the answer is recovery,
    never a rerun. Same shape as ``ABORTED`` plus a recorded defect, so it is the largest
    of the four and the one the gate is really decided on.

WHAT MAKES THE PROJECTION FIT
-----------------------------
Not a budget raise, and not a deletion. Only:

* ``next_milestone`` is PROSPECTIVE by construction — generation 13's described S3Y, which
  by the time generation 14 is written has HAPPENED. Its replacement describes what comes
  after a spent holdout, and it is written tight because the permanent rules it would
  otherwise restate live on ``frozen_invariants``, which is where permanent rules belong.
* Limitation entries whose subject generation 14 genuinely SETTLES are rewritten to the
  settled fact rather than kept as an open question beside its own answer. "Candidate
  004's eligibility is UNKNOWN until eval-v6 is spent exactly once" is superseded by
  eval-v6 having been spent exactly once; keeping both is duplication, not history.
* The same is true of one FROZEN INVARIANT. ``GEN11 IS READINESS, NOT AUTHORITY`` asserted
  "no human authorised evaluation, no holdout is spent"; generation 14 falsifies both in
  ALL FOUR endings. Shipping it unchanged would emit a snapshot contradicting its own
  ``spent_by``, so it is rewritten to the settled fact. That is truthfulness, not room.

Every clause that must survive that compaction is listed in :data:`CARRIED_FORWARD` and
checked HERE, fail-closed, before the projection is measured. A compaction that drops a
standing prohibition is refused rather than reported.

WHY THE STAND-INS ARE THE POINT (S3Y.CAP1)
------------------------------------------
The first version of this projector defaulted ``passed``/``skipped``/``failed`` to ``0``.
One digit each — where a real baseline carries four and two. Every projection therefore
came out FOUR BYTES SMALLER than any snapshot that could actually be written, and four
bytes was the whole margin: the worst-case ending reported 1027 spare against a 1024
floor, so the truthful figure was 1023 and the gate was green while measuring a fiction.

A stand-in of the wrong SHAPE is worse than no stand-in at all, because it prints PASS. So
every value this projector cannot know before S3Y runs is now either a fixed-length
literal, or a RIGHT-SIZED bounded maximum that is refused if the real value outgrows it:
:func:`assert_baseline_within_standin` and :func:`assert_spend_fields_within_standin` fail
closed rather than under-measure.

NOTHING HERE WRITES STATE UNLESS ``--emit`` IS PASSED. Nothing here reads a held-out task
body, loads a model, touches an adapter, generates a token, creates or requests an
authority, or names a candidate-004 RESULT: the projected verdicts are SHAPES, and the
placeholder digests are stand-ins of the right size, which is what makes this a PREFLIGHT.
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

#: The per-entry character cap the verifier enforces on every snapshot string. Mirrored
#: here so a projection fails before it is written rather than after it is reviewed.
MAX_ENTRY_CHARS = 320

#: Right-SIZED stand-ins for the fields whose value is not known until S3Y has run.
#:
#: A stand-in of the wrong SHAPE is worse than no stand-in, because it produces a
#: projection that is SMALLER than any truthful snapshot and prints PASS. That is exactly
#: what ``passed=skipped=failed=0`` did: one digit each, where the real baseline carries a
#: four-digit pass count and a two-digit skip count. Those four bytes were the whole
#: margin -- the worst-case ending measured 1027 spare against a 1024 floor, so the
#: truthful figure was 1023 and the gate was green only because it was measuring a
#: fiction.
#:
#: These are the canonical BOUNDED MAXIMA for the digit widths, not today's values: the
#: suite may grow, and a projection that has to be re-argued every time a test is added is
#: not a bound. ``assert_baseline_within_standin`` refuses any real count wider than what
#: was projected.
STANDIN_PASSED = 99_999      # 5 digits
STANDIN_SKIPPED = 999        # 3 digits
STANDIN_FAILED = 999         # 3 digits

#: The short digests the spend record interpolates are EIGHT hex characters, as every
#: historical ``spent_by`` shows. Nothing used to enforce that: the full ``plan_hash`` is
#: 64 hex, so passing it -- the natural mistake -- silently added 112 bytes to a
#: projection whose whole margin is smaller than that.
SHORT_DIGEST_CHARS = 8

#: ``evaluation_id`` and the generation ordinal are interpolated into ``spent_by`` too.
#: Both are bounded so the projection cannot be quietly outgrown by its own inputs.
MAX_EVALUATION_ID_CHARS = 32
MAX_EVALUATION_GENERATION = 9

EXPECTED_PARENT_SHA256 = (
    "9f49c759b32c571b05b285be9da210da6a609c0aaea6e059010b07bcf2dc6f6c")
EXPECTED_PARENT_GENERATION = 13

GEN14_MILESTONE = "S3Y"
GEN14_EVIDENCE = "jarvis/docs/V69_M62_S3Y_CANDIDATE004_LIVE_HELDOUT_EVALUATION.md"

CANDIDATE_ID = "qwen3-06b-lora-quality-live-004"
EVAL_RECEIPT_PATH = "state/m62/receipts/qwen3-06b-lora-quality-live-004.eval.json"
EVAL_V6_MANIFEST = (
    "413e675711d51f5b98cb5a8ec7ff7fb0d8eb36b5e4c6dff790fb60f764f8fba6")

TERMINAL_STATES = ("ELIGIBLE", "NOT_ELIGIBLE", "ABORTED", "DURABILITY_FAILURE")

GEN14_LABELS = {
    "ELIGIBLE": "M62_S3Y_CANDIDATE004_EVALUATED_ELIGIBLE",
    "NOT_ELIGIBLE": "M62_S3Y_CANDIDATE004_EVALUATED_NOT_ELIGIBLE",
    "ABORTED": "M62_S3Y_CANDIDATE004_EVALUATION_ABORTED",
    "DURABILITY_FAILURE": "M62_S3Y_CANDIDATE004_EVALUATION_DURABILITY_FAILURE",
}

#: Clauses that MUST still appear somewhere in the projected snapshot.
#:
#: The failure mode of a compaction is never that the rewrite is wrong — it is that a
#: rewrite aimed at one clause quietly drops four others nobody was thinking about. Each
#: entry is ``(substring, surface)``; the substring is searched in the joined text of that
#: surface, so wording stays free and coverage does not. This is the machine-checked half
#: of "lossless"; :mod:`verify_m62_control_plane` independently re-checks the standing
#: ``ruled_out`` subjects, the retirement markers and the per-entry cap.
CARRIED_FORWARD: tuple[tuple[str, str], ...] = (
    # The eval-v5 retirement, in every place the firewall carries it. A generation that
    # spends eval-v6 is the likeliest one to get sloppy about the corpus it did NOT spend.
    ("ELIGIBILITY_USE: RETIRED", "frozen_invariants"),
    ("FRESH_V6_REQUIRED", "frozen_invariants"),
    ("NO MODEL EVER SAW IT", "frozen_invariants"),
    ("eval-v5", "next_milestone.ruled_out"),
    # Body-blindness, the gate that failed once and may not be relaxed.
    ("ORCHESTRATOR BODY-BLINDNESS IS A GATE", "frozen_invariants"),
    ("repr of a bound method", "frozen_invariants"),
    ("A HOLDOUT AUTHOR IS NEVER ITS EVALUATOR", "frozen_invariants"),
    # The four-events invariant, which a spend generation is the likeliest to blur.
    ("PLAN CONSUMED != HOLDOUT SPENT", "frozen_invariants"),
    ("RERUN IS FORBIDDEN", "frozen_invariants"),
    ("never a rerun", "frozen_invariants"),
    ("REDERIVED", "frozen_invariants"),
    ("GEN11 IS READINESS, NOT AUTHORITY", "frozen_invariants"),
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
    ("learning-rate", "next_milestone.ruled_out"),
    ("structured rows", "next_milestone.ruled_out"),
    ("response schema", "next_milestone.ruled_out"),
    ("D38 gate", "next_milestone.ruled_out"),
    ("D35", "next_milestone.holdout_access"),
    # The budget rule itself, which a capacity milestone is the likeliest to erode.
    ("raising a reviewed budget", "next_milestone.ruled_out"),
    # S3Y'S OWN ADDITIONS. A spent single-use holdout creates three prohibitions that did
    # not exist while it was frozen, and they are checked from the generation that
    # creates them rather than from the one that would first violate them.
    ("eval-v6", "next_milestone.ruled_out"),
    ("second", "next_milestone.ruled_out"),
    ("eval-v7", "next_milestone.ruled_out"),
    ("candidate 005", "next_milestone.ruled_out"),
    # The merged limitations. Every subject that entered a merge must leave it.
    ("promotion_plan_hash", "limitations"),
    ("openai", "limitations"),
    ("transformers", "limitations"),
    ("timeout_enforced false", "limitations"),
    ("authored eval-v5", "limitations"),
    ("authored eval-v6", "limitations"),
    ("D44 exposure is PERMANENT", "limitations"),
    ("VACUOUS", "limitations"),
    ("thresholds_are_calibrated is false", "limitations"),
    ("Semantic leakage has never run", "limitations"),
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


def _replace_prefix(entries: list[str], prefix: str, text: str) -> None:
    """Rewrite the one entry beginning with ``prefix``, refusing if it is not unique.

    Addressed by PREFIX rather than by index deliberately. Generation 13 compacted its own
    limitation list, so every index shifted; an index-addressed edit written against the
    wrong parent rewrites whatever happens to sit at that position, silently. A prefix
    that matches zero or two entries is a refusal instead.
    """
    hits = [i for i, entry in enumerate(entries) if entry.startswith(prefix)]
    if len(hits) != 1:
        raise RuntimeError(
            f"{len(hits)} entries start with {prefix!r}; the parent snapshot is not the "
            f"one this projection was written against")
    entries[hits[0]] = text


def _entry(payload: dict, surface: str, key: str, value: str) -> dict:
    """The one member of a snapshot list whose ``key`` is ``value``, or a refusal."""
    hits = [x for x in payload[surface] if x.get(key) == value]
    if len(hits) != 1:
        raise RuntimeError(
            f"{len(hits)} {surface} entries have {key}={value!r}; the parent snapshot is "
            f"not the one this projection was written against")
    return hits[0]


def assert_baseline_within_standin(passed: int, skipped: int, failed: int) -> None:
    """Refuse a real test baseline wider than the one capacity was proved against.

    The projection is a claim about a SHAPE. If the real baseline needs more digits than
    the stand-in reserved, the bytes measured here are not the bytes that would land, and
    the gate proved nothing about the snapshot actually being written.
    """
    for name, value, bound in (("passed", passed, STANDIN_PASSED),
                               ("skipped", skipped, STANDIN_SKIPPED),
                               ("failed", failed, STANDIN_FAILED)):
        if value < 0 or len(str(value)) > len(str(bound)):
            raise RuntimeError(
                f"test_baseline.{name}={value} does not fit the projected stand-in width "
                f"of {len(str(bound))} digits; capacity was not proved for it")


def assert_spend_fields_within_standin(evaluation_id: str, plan_digest: str,
                                       report_digest: str,
                                       evaluation_generation: int) -> None:
    """Refuse any spend-record field longer than the projection assumed."""
    if len(evaluation_id) > MAX_EVALUATION_ID_CHARS:
        raise RuntimeError(
            f"evaluation_id is {len(evaluation_id)} characters, over the projected "
            f"{MAX_EVALUATION_ID_CHARS}")
    for name, digest in (("plan_digest", plan_digest),
                         ("report_digest", report_digest)):
        if len(digest) != SHORT_DIGEST_CHARS:
            raise RuntimeError(
                f"{name} is {len(digest)} characters; the spend record interpolates the "
                f"{SHORT_DIGEST_CHARS}-character SHORT digest, and a full 64-hex hash "
                f"would add bytes this projection never measured")
    if not 1 <= evaluation_generation <= MAX_EVALUATION_GENERATION:
        raise RuntimeError(
            f"evaluation generation {evaluation_generation} is outside the projected "
            f"range 1..{MAX_EVALUATION_GENERATION}")


def project_gen14(parent: dict, *, terminal_state: str, subject_commit: str,
                  parent_sha256: str, evaluation_id: str, plan_digest: str,
                  report_digest: str, passed: int, skipped: int,
                  failed: int, evaluation_generation: int = 1) -> dict:
    """Generation 13 -> the minimum truthful generation 14. Pure; writes nothing."""
    if terminal_state not in TERMINAL_STATES:
        raise RuntimeError(f"unknown terminal state {terminal_state!r}")
    assert_baseline_within_standin(passed, skipped, failed)
    assert_spend_fields_within_standin(
        evaluation_id, plan_digest, report_digest, evaluation_generation)
    g = json.loads(json.dumps(parent))
    measured = terminal_state in ("ELIGIBLE", "NOT_ELIGIBLE")

    g["state_generation"] = 14
    g["generation_label"] = GEN14_LABELS[terminal_state]
    g["subject_state_milestone"] = GEN14_MILESTONE
    g["subject_state_commit"] = subject_commit
    g["parent_snapshot_sha256"] = parent_sha256

    # ── eval-v6 is SPENT in all four states ──────────────────────────────────────
    #
    # Including the two that did not finish. The spend boundary is the durable
    # holdout_model_facing_committed event and NOT proof a forward pass completed, so an
    # abort after that point spends the corpus exactly as a clean run does. Recording it
    # any other way would manufacture a fresh holdout out of a failure.
    v6 = _entry(g, "datasets", "manifest_hash", EVAL_V6_MANIFEST)
    if v6["status"] != "FROZEN_UNUSED" or v6["spent_by"] is not None:
        raise RuntimeError("eval-v6 is not FROZEN_UNUSED and unspent in the parent")
    v6["status"] = "USED_IMMUTABLE"
    v6["evidence"] = GEN14_EVIDENCE
    v6["spent_by"] = (
        f"S3Y LIVE, candidate 004 (evaluation {evaluation_id} "
        f"gen-{evaluation_generation}, plan {plan_digest}, report {report_digest})")

    # ── eval-v5 is NOT touched, in any state ─────────────────────────────────────
    v5 = _entry(g, "datasets", "version", "v5")
    if v5["status"] != "FROZEN_UNUSED" or v5["spent_by"] is not None:
        raise RuntimeError("eval-v5 must stay FROZEN_UNUSED with spent_by null")

    # ── candidate 004 ────────────────────────────────────────────────────────────
    c4 = _entry(g, "candidates", "candidate_id", CANDIDATE_ID)
    if c4["status"] != "TRAINED_UNEVALUATED":
        raise RuntimeError("candidate 004 is not TRAINED_UNEVALUATED in the parent")
    c4["evidence"] = GEN14_EVIDENCE
    if measured:
        c4["status"] = ("EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW"
                        if terminal_state == "ELIGIBLE" else "EVALUATED_NOT_ELIGIBLE")
        c4["evaluation_corpus"] = "m62-defensive-eval v6"
        c4["evaluation_receipt"] = EVAL_RECEIPT_PATH
    # ABORTED and DURABILITY_FAILURE leave all three fields alone. An EVALUATED_* state
    # REQUIRES a portable receipt and is REDERIVED from it; without one the honest record
    # is a candidate still TRAINED_UNEVALUATED beside a holdout that is already spent.

    g["control_plane_note"] = {
        "ELIGIBLE":
            "S3Y spent eval-v6 EXACTLY ONCE on candidate 004 under one human EVAL "
            "authority, now consumed. Every gate passed: candidate 004 is "
            "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW, which REQUESTS a human decision and is "
            "not one. No promotion authority exists. eval-v5 stays FROZEN_UNUSED, "
            "spent_by null, RETIRED.",
        "NOT_ELIGIBLE":
            "S3Y spent eval-v6 EXACTLY ONCE on candidate 004 under one human EVAL "
            "authority, now consumed. At least one gate failed: candidate 004 is "
            "EVALUATED_NOT_ELIGIBLE, and the result is final because no rerun or second "
            "look is possible. eval-v5 stays FROZEN_UNUSED, spent_by null, RETIRED.",
        "ABORTED":
            "S3Y began model-facing access to eval-v6 and did NOT complete. eval-v6 is "
            "USED_IMMUTABLE from the durable commit regardless; with no portable receipt "
            "candidate 004 stays TRAINED_UNEVALUATED. Rerun is forbidden; only recovery "
            "remains. eval-v5 stays FROZEN_UNUSED, spent_by null, RETIRED.",
        # The retention-and-recovery rule is a frozen invariant and is not restated here.
        "DURABILITY_FAILURE":
            "S3Y measured candidate 004 against eval-v6 and the durable evidence did NOT "
            "land (D45). eval-v6 is USED_IMMUTABLE and candidate 004 stays "
            "TRAINED_UNEVALUATED with no receipt. eval-v5 stays FROZEN_UNUSED, spent_by "
            "null, RETIRED.",
    }[terminal_state]

    # ── Defects: one addition, and only in the state that earns it ───────────────
    if terminal_state == "DURABILITY_FAILURE":
        g["defects"].append({
            "id": "D45",
            "is_gate": False,
            "status": "OPEN",
            # The RULE -- artefacts retained, recovery never a rerun -- is already a
            # frozen invariant. What a defect records is the INSTANCE, so this says what
            # happened and points at the rule rather than restating it.
            "summary":
                "A valid S3Y measurement of candidate 004 against eval-v6 was taken and "
                "its durable evidence did not land. eval-v6 stays spent; the retention "
                "and recovery-not-rerun invariant governs.",
            "evidence": GEN14_EVIDENCE,
        })

    # ── Limitations: settle what generation 14 settles; add what it creates ──────
    #
    # Every edit below is one of exactly three kinds, which is what makes the compaction
    # lossless rather than a quiet deletion:
    #   (a) a PROSPECTIVE clause whose subject generation 14 settles, rewritten to the
    #       settled fact -- keeping both would be duplication, not history;
    #   (b) two entries that already carried ONE subject, merged, with every fact kept;
    #   (c) a clause that generation 13 moved onto `frozen_invariants`, no longer
    #       restated at length on the limitation that used to carry it.
    # Nothing is dropped for space. CARRIED_FORWARD re-checks the survivors fail-closed.
    lim = g["limitations"]
    generated = terminal_state != "ABORTED"

    # (a) The one word that changes: eval-v6 stops being the holdout 004 is measured on
    #     NEXT. Deliberately NOT re-stated with the baseline-arm clause, which the entry
    #     below it already carries.
    _replace_prefix(
        lim, "Candidates 001-004 were each measured",
        "Candidates 001-004 were each measured on a DIFFERENT holdout (001 eval-v2, 002 "
        "eval-v3, 003 eval-v4, 004 eval-v6) sharing ZERO task instances, and 003 onward "
        "is fitted under DISABLED. No cross-candidate table is a head-to-head ranking.")

    # (b) The two entries that both bound candidate 004's single-axis claim.
    _replace_prefix(
        lim, "Candidate 004's single-axis proof",
        "Candidate 004's single-axis proof covers the CONFIGURATION, not trained "
        "weights, and the learning rate had never been varied in this lineage: it adds "
        "one point, not a dose-response curve. S3S.1 rated the risk to candidate 003's "
        "three security gains from a weaker update as HIGH.")
    _replace_prefix(lim, "The learning rate had never been varied", "")

    # (b) D28/D29/D33 already carried the never-enforced timeout; the S3Q.0 surface note
    #     is the same fact stated twice.
    _replace_prefix(
        lim, "D28, D29 and D33 stay OPEN",
        "D28, D29 and D33 stay OPEN and bound every measurement: tool_call_validity_rate "
        "is VACUOUS so the six tool_call_schema tasks decide nothing; timeout_s is hashed "
        "but never enforced, so timeout_rate is VACUOUS and surfaces quoting the 300 s "
        "ceiling report timeout_enforced false; D29 bounds refusal figures BOTH ways.")
    _replace_prefix(lim, "Every S3Q.0 surface quoting", "")

    # (a) The boundary stopped being PROSPECTIVE the moment S3Y crossed it.
    _replace_prefix(
        lim, "The prospective spend boundary",
        "The spend boundary is EARLIER than proof a forward pass ran: no atomic "
        "transaction spans a durable local append and an external synchronous call, so a "
        "crash in that gap marks spent a holdout no model may have read. The "
        "conservative error is chosen.")

    # (c) Generation 13 put "A HOLDOUT AUTHOR IS NEVER ITS EVALUATOR" on the invariant
    #     surface, where permanent rules belong. What stays here is the candidate-004
    #     instance of it, not a second copy of the rule.
    _replace_prefix(
        lim, "The session that authored eval-v5",
        "The session that authored eval-v5 is disqualified from designing candidate 004, "
        "and the one that authored eval-v6 from evaluating it.")

    if generated:
        # (a) Both entries said "nothing has been loaded or generated yet". S3Y is that
        #     yet. The caveats that OUTLIVE the loading are what remain.
        _replace_prefix(
            lim, "No model has been generated under the D38 instrument",
            "Every D38 figure S3M.2 reports is synthetic or a re-reading of sealed "
            "metadata; S3Y is the first real generation under that instrument.")
        _replace_prefix(
            lim, "The adapter, base weights and runtime were qualified",
            "S3W.0 qualified the adapter, base weights and runtime from metadata, "
            "digests and safetensors headers alone; S3Y is the first load of them.")
    if measured:
        _replace_prefix(
            lim, "m62.eval_receipt.3 has described exactly ONE",
            "m62.eval_receipt.3 has described TWO real evaluations; its Unicode "
            "handling, four-verdict partition and source split are proved against those "
            "plus synthetic non-vacuity. .2 remains proved over synthetic evidence only "
            "and stays a valid contract.")

    if measured:
        _replace_prefix(
            lim, "A successful S3V means",
            "Candidate 004's result is ONE run: one host, one CPU, one seed, 36 "
            "synthetic tasks by one author. Train and validation loss were never "
            "eligibility evidence. It is final because eval-v6 is spent - no ablation, "
            "retry, re-score or second look is possible.")
    else:
        # ONE entry, not two. "Candidate 004 is unmeasured beside a spent holdout" and
        # "no fresh eligibility corpus is left" are the same fact stated from two ends;
        # splitting them would put the second half of a sentence on its own surface.
        _replace_prefix(
            lim, "A successful S3V means",
            "Candidate 004 is TRAINED and STILL UNMEASURED yet eval-v6 is SPENT: S3Y "
            "reached the durable model-facing commit without a portable receipt. NO "
            "fresh eligibility corpus is left - eval-v4 and eval-v6 spent, eval-v5 "
            "retired unspent - so any future claim needs a NEW holdout and a fresh "
            "operator ruling.")

    _replace_prefix(
        lim, "eval-v4 is spent",
        "eval-v4 is spent and so, from S3Y, is eval-v6: no ablation, retry or re-scoring "
        "of candidate 003 or 004 against either is possible. D35's fresh-holdout "
        "requirement passed from eval-v4 to eval-v5 to eval-v6, which discharged it "
        "exactly once.")

    if terminal_state == "ELIGIBLE":
        lim.append(
            "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW IS A REQUEST, NOT A RESULT ANYONE MAY "
            "ACT ON: the frozen gates were cleared on one uncalibrated 36-task holdout. "
            "Not production-readiness, not a promotion, not permission to seek one.")
    elif terminal_state == "NOT_ELIGIBLE":
        lim.append(
            "Candidate 004 is NOT ELIGIBLE on the frozen gates, measured once against "
            "eval-v6. Those gates are uncalibrated and the holdout is 36 synthetic "
            "tasks, so this bounds the learning-rate axis on THIS instrument only.")
    # ABORTED / DURABILITY_FAILURE append nothing: the entry above already carries both
    # halves of what those states add.

    g["limitations"] = [x for x in lim if x]

    # ── Frozen invariants: one PROSPECTIVE clause that S3Y settles ───────────────
    #
    # (a), applied to the invariant surface. Generation 13 recorded "no EVAL capability
    # exists, no human authorised evaluation, no holdout is spent and no candidate has
    # passed or failed". Every one of those clauses is FALSE the moment S3Y crosses the
    # model-facing boundary -- in ALL FOUR endings, because the spend boundary is the
    # durable commit and not proof a forward pass finished. Shipping it unchanged would
    # emit a snapshot asserting "no holdout is spent" beside a spent_by naming what spent
    # it, which is not a compaction question but a truthfulness one.
    #
    # What the invariant is FOR -- qualification is not authorisation -- is permanent and
    # is kept verbatim as the leading clause CARRIED_FORWARD pins.
    _replace_prefix(
        g["frozen_invariants"], "GEN11 IS READINESS, NOT AUTHORITY",
        "GEN11 IS READINESS, NOT AUTHORITY. A qualified ceremony is not an authorised "
        "one. S3Y consumed ONE single-use human EVAL authority and spent eval-v6; "
        "neither is reusable.")

    # ── The prospective contract, rewritten for the milestone that has not happened ──
    if measured:
        # The axis is CLOSED. It still has to name both ends and the render baseline the
        # verifier re-derives (MODEL_DEFAULT -> DISABLED), because an axis recorded
        # without its two ends is not a measurable claim.
        primary_axis = (
            "MEASURED and CLOSED. Candidate 004's axis was learning_rate 1e-4 -> 5e-5, "
            "alpha not slaved, on candidate 003's MODEL_DEFAULT -> DISABLED render "
            "baseline (D37). Spent once against eval-v6; it cannot be reopened.")
    else:
        # Still OPEN, and it must still name the dial and BOTH ends -- the verifier
        # re-derives them from the production generator. What it no longer needs is the
        # sentence saying loss is not evidence, which the limitation surface carries.
        primary_axis = (
            "Candidate 004's learning_rate axis against candidate 003, 1e-4 -> 5e-5, "
            "alpha not slaved so alpha/r stays 2.0, is TRAINED and STILL UNMEASURED "
            "after S3Y spent eval-v6. Eligibility is UNKNOWN.")

    # Written DENSE on purpose. Generation 13 already established that the permanent
    # rules this list used to restate at length live on `frozen_invariants`, which is
    # where permanent rules belong; this list carries the PROSPECTIVE bars only. The
    # subjects the verifier requires, and every clause in CARRIED_FORWARD, survive as
    # substrings -- coverage is fixed, wording is not.
    common_ruled_out = [
        "any second look at eval-v6: rerun, ablation, re-score, alternate seed, partial "
        "re-measurement, or reading, quoting or reconstructing one of its bodies. It is "
        "USED_IMMUTABLE and single-use, and a bad or missing result is not permission to "
        "look again",
        "using eval-v5 as eligibility evidence, marking it USED_IMMUTABLE or setting "
        "spent_by non-null; or reusing, reading or reconstructing eval-v4, eval-v5 or "
        "leaked material in any session",
        "creating eval-v7 or any replacement holdout, or designing, naming or "
        "configuring a candidate 005, without a separate explicit operator ruling",
        "recording candidate 004 as evaluated, eligible or promoted without a portable "
        "receipt re-derived by production decide_eligibility",
        "relaxing body-blindness, or any preregistered gate, grader or threshold, now "
        "that a result is known, which is post-hoc weakening",
        "any epoch, rank, alpha, dropout or module-surface change, a dial slaved to the "
        "learning rate, a second axis, ATTENTION_ONLY, train-v3, structured rows, a "
        "stronger response schema, or a changed refusal detector, max_new_tokens, seed "
        "or reasoning policy, or creating a D38 gate or a D43 gate",
        "reading candidate 004's learning-rate permission as general: the S3U ruling "
        "superseded the generation-8 clause for candidate 004 only, prospectively, and "
        "only to 5e-5",
        "raising a reviewed budget, or deleting recorded defects, limitations or "
        "invariants to make room",
        "retraining, resuming, re-seeding or patching candidate 003 or 004, calling "
        "either production-ready, or fixing D39 as a rider",
        "promotion, activation, registry mutation, merge, tag, release or version bump "
        "without a separate explicit human authority, which no result implies",
    ]

    names = {
        "ELIGIBLE":
            "S3Z an explicit human decision on candidate 004's ELIGIBLE measurement: "
            "promote, hold or reject. The measurement requests that decision and does "
            "not make it.",
        "NOT_ELIGIBLE":
            "S3Z an explicit operator ruling on what, if anything, follows a NOT "
            "ELIGIBLE candidate 004 with no fresh holdout left. Nothing is designed or "
            "named before it.",
        "ABORTED":
            "S3Z operator-governed RECOVERY of the aborted S3Y run: establish from "
            "retained artefacts what was measured, if anything. Recovery is not a rerun.",
        "DURABILITY_FAILURE":
            "S3Z operator-governed RECOVERY of the S3Y measurement whose evidence did "
            "not land (D45), from retained artefacts only. Recovery is not a rerun.",
    }

    g["next_milestone"] = {
        "name": names[terminal_state],
        "requires_new_session": True,
        "primary_axis": primary_axis,
        "lora_scope": "ATTENTION_AND_MLP",
        "training_corpus": "m62-defensive-quality-train v2, spent on candidates 003 and "
                           "004, reused UNCHANGED. No train-v3 exists; S3Z trains "
                           "nothing.",
        "evaluation_holdout": "eval-v6 is USED_IMMUTABLE, spent by S3Y on candidate 004. "
                              "NO fresh eligibility corpus exists: eval-v4 and eval-v6 "
                              "are spent, eval-v5 RETIRED unspent.",
        "holdout_access": "No holdout may be rendered to any model or session. eval-v6 "
                          "is spent and joins eval-v4 as development evidence under D35; "
                          "its bodies and eval-v5's stay unread permanently. D44's gate "
                          "applies unchanged.",
        "authority_required": [
            "a fresh single-use human EVAL authority, form EVAL:<plan-hash>, before any "
            "further evaluation; the S3Y one is SPENT and no corpus exists to name",
            "a fresh single-use human TRAIN authority, form TRAIN:<plan-hash>, before "
            "any further training; the S3V one is SPENT",
            "a separate explicit human decision before any promotion, which no result "
            "implies",
        ],
        "ruled_out": common_ruled_out,
    }

    g["test_baseline"] = dict(g["test_baseline"])
    g["test_baseline"]["milestone"] = GEN14_MILESTONE
    g["test_baseline"]["passed"] = passed
    g["test_baseline"]["skipped"] = skipped
    g["test_baseline"]["failed"] = failed

    # The 320-character per-entry cap is a FIREWALL, not a style rule: it is what stops a
    # held-out body being smuggled into state one instalment at a time. The verifier
    # enforces it, and so does this, so a projection that would violate it never reaches
    # the point of being measured, emitted or reviewed.
    too_long = [f"{surface}[{i}] is {len(text)} characters"
                for surface in ("frozen_invariants", "limitations")
                for i, text in enumerate(g[surface]) if len(text) > MAX_ENTRY_CHARS]
    too_long += [f"next_milestone.ruled_out[{i}] is {len(text)} characters"
                 for i, text in enumerate(g["next_milestone"]["ruled_out"])
                 if len(text) > MAX_ENTRY_CHARS]
    if too_long:
        raise RuntimeError(
            f"entries exceed the {MAX_ENTRY_CHARS}-character control-plane cap: "
            f"{too_long}")

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
                        help="the commit generation 14 is a claim about")
    parser.add_argument("--evaluation-id", default="m62-s3y-quality-heldout-live",
                        help="the evaluation output-root identity the spend names")
    parser.add_argument("--plan-digest", default="0" * 8,
                        help="short plan digest; a stand-in of the right SHAPE until the "
                             "plan exists, which is what makes this a PREFLIGHT")
    parser.add_argument("--report-digest", default="0" * 8,
                        help="short report digest; a stand-in of the right SHAPE")
    parser.add_argument("--evaluation-generation", type=int, default=1,
                        help="the evaluation generation ordinal the spend record names")
    parser.add_argument("--passed", type=int, default=STANDIN_PASSED,
                        help="real focused-M62 pass count; defaults to the RIGHT-SIZED "
                             "stand-in, never to a narrower fiction")
    parser.add_argument("--skipped", type=int, default=STANDIN_SKIPPED)
    parser.add_argument("--failed", type=int, default=STANDIN_FAILED)
    parser.add_argument("--terminal-state", default="", choices=("",) + TERMINAL_STATES,
                        help="project ONE state; default projects all four")
    parser.add_argument("--emit", default="",
                        help="write the projected generation 14 to this path. Requires "
                             "--terminal-state and real digests")
    parser.add_argument("--parent", default="",
                        help="the generation 13 snapshot to project from. Defaults to "
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

    print(f"PARENT_GENERATION: {parent['state_generation']}")
    print(f"PARENT_SNAPSHOT_BYTES: {len(parent_bytes)}")
    print(f"PARENT_SNAPSHOT_HEADROOM: {SNAPSHOT_MAX_BYTES - len(parent_bytes)}")
    print(f"SNAPSHOT_BUDGET_BYTES: {SNAPSHOT_MAX_BYTES}")
    print(f"REQUIRED_HEADROOM_BYTES: {REQUIRED_HEADROOM_BYTES}")

    wanted = (args.terminal_state,) if args.terminal_state else TERMINAL_STATES
    projections: dict[str, dict] = {}
    worst = SNAPSHOT_MAX_BYTES
    for state in wanted:
        projected = project_gen14(
            parent, terminal_state=state, subject_commit=args.subject_commit,
            parent_sha256=parent_sha, evaluation_id=args.evaluation_id,
            plan_digest=args.plan_digest, report_digest=args.report_digest,
            passed=args.passed, skipped=args.skipped, failed=args.failed,
            evaluation_generation=args.evaluation_generation)
        projections[state] = projected
        size, head = measure(projected)
        worst = min(worst, head)
        print(f"GEN14_{state}_BYTES: {size}")
        print(f"GEN14_{state}_HEADROOM: {head}")
        print(f"GEN14_{state}_CAPACITY: "
              f"{'PASS' if head >= REQUIRED_HEADROOM_BYTES else 'FAIL'}")
        print(f"GEN14_{state}_LOSSLESS: "
              f"{'PASS' if not check_carried_forward(projected) else 'FAIL'}")

    p_bytes, p_head, p_lines, p_line_head = progress_headroom()
    print(f"GEN14_WORST_CASE_HEADROOM: {worst}")
    print(f"CARRIED_FORWARD_CLAUSES: {len(CARRIED_FORWARD)}")
    print(f"PROGRESS_BYTES: {p_bytes} HEADROOM: {p_head}")
    print(f"PROGRESS_LINES: {p_lines} HEADROOM: {p_line_head}")
    print(f"PROGRESS_HEADROOM_INVARIANT: "
          f"{'PASS' if p_head > 0 and p_line_head > 0 else 'FAIL'}")

    ok = worst >= REQUIRED_HEADROOM_BYTES and p_head > 0 and p_line_head > 0
    print(f"S3Y_GEN14_CAPACITY: {'PASS' if ok else 'BLOCKED'}")

    if args.emit and not ok:
        # Fail closed. A snapshot written past its own gate is a state nobody proved room
        # for, and the next generation is the one that pays for it.
        print("EMIT_REFUSED: the capacity gate did not pass")
        return 1
    if args.emit:
        if not args.terminal_state:
            print("EMIT_REFUSED: --emit needs exactly one --terminal-state")
            return 1
        if args.plan_digest == "0" * 8 or args.report_digest == "0" * 8:
            print("EMIT_REFUSED: refusing to write a snapshot carrying a stand-in digest")
            return 1
        if (args.passed, args.skipped, args.failed) == (
                STANDIN_PASSED, STANDIN_SKIPPED, STANDIN_FAILED):
            # The stand-in is a BOUND, not a measurement. A snapshot carrying it would
            # record a test baseline nobody ran.
            print("EMIT_REFUSED: refusing to write a snapshot carrying the stand-in "
                  "test baseline")
            return 1
        if args.subject_commit == "0" * 40:
            print("EMIT_REFUSED: refusing to write a snapshot with a stand-in commit")
            return 1
        payload = projections[args.terminal_state]
        Path(args.emit).write_bytes(canonical_bytes(payload))
        print(f"EMITTED: {args.emit}")
        print(f"EMITTED_SHA256: {sha256_bytes(canonical_bytes(payload))}")

    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
