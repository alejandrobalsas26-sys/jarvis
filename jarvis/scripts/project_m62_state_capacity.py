"""scripts/project_m62_state_capacity.py — V69 M62 S3W.0: does the next state still fit?

WHY THIS EXISTS
---------------
The control plane's snapshot budget is 32 768 bytes and generation 10 closed at 31 607 —
1 161 bytes of headroom. The next two generations both have to carry MORE truth than that
one: generation 11 records that the evaluation ceremony is qualified, and a future
generation 12 has to record what an evaluation of candidate 004 actually found. Writing
generation 11 first and discovering at generation 12 that the result does not fit would
leave the repository with nowhere to put a measured outcome — and the outcome is the one
thing that may never be dropped, summarised away or moved to a document.

So capacity is proved BEFORE generation 11 is written, for BOTH generations, and the
projection is not a guess: :func:`project_gen11` is the same transform ``--emit`` writes,
so the bytes that are measured here are the bytes that land on disk.

WHAT THE GENERATION 12 PROJECTION IS
------------------------------------
A deliberately CONSERVATIVE worst case, not a prediction. It assumes the longest of the
truthful outcomes — a candidate that is measured NOT eligible, which carries a blocking
gate summary, a regression summary AND a security summary, where an eligible one would
carry less — and it uses the widest real spent_by string the repository has ever written
(S3Q's, which names an evaluation, a generation, a plan and a report). It contains no
eval-v5 body, no real result and no predicted figure: every value is either a length
stand-in of the right shape or a fact already true at generation 10.

NOTHING HERE WRITES STATE UNLESS ``--emit`` IS PASSED, and nothing here ever reads a
held-out task body, loads a model or touches an adapter.
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

#: The headroom both projected generations must clear. Not the schema budget: a snapshot
#: that fits with nothing to spare is one truthful sentence away from not fitting.
REQUIRED_HEADROOM_BYTES = 1024

GEN11_LABEL = "M62_S3W0_CANDIDATE004_EVAL_READY"
GEN11_MILESTONE = "S3W.0"

#: The authoritative focused baseline measured at S3W.0, on the invocation and interpreter
#: generation 10 already records. The counts MOVE as tests are added -- 4234 -> 4284 is the
#: S3W.0 suite -- so what is pinned is the invocation and ZERO failures, never the total.
GEN11_TEST_BASELINE_PASSED = 4284
GEN11_TEST_BASELINE_SKIPPED = 20

#: The parent generation 11 chains onto. Passed in rather than discovered, so a run
#: against a control plane somebody else advanced fails loudly instead of re-parenting.
EXPECTED_PARENT_SHA256 = (
    "b36b13baf4c9624e6045450737256db95421625c86c6659b0e531731553da075")


# ── generation 11 ────────────────────────────────────────────────────────────────────
def project_gen11(parent: dict, *, subject_commit: str, parent_sha256: str,
                  test_baseline: dict | None = None) -> dict:
    """The EVAL_READY state. Readiness only — it asserts no authority and no result.

    Every candidate and dataset fact is carried through unchanged: S3W.0 measured them,
    it did not move them. What changes is the note, the label, the subject, the forward
    plan, and the limitations a qualification session is obliged to record.
    """
    state = json.loads(json.dumps(parent))  # deep copy; the parent is never mutated

    state["state_generation"] = parent["state_generation"] + 1
    state["generation_label"] = GEN11_LABEL
    state["subject_state_milestone"] = GEN11_MILESTONE
    state["subject_state_commit"] = subject_commit
    state["parent_snapshot_sha256"] = parent_sha256
    state["control_plane_note"] = (
        "S3W.0 qualified the candidate 004 eval ceremony BODY-FREE and evaluated "
        "nothing: 0 weight loads, 0 generations, 0 eval attempts, 0 holdout spends, "
        "0 EVAL authority created. eval-v5 stays FROZEN_UNUSED, spent_by null, bodies "
        "unread. EVAL and promotion authority: NONE.")

    state["limitations"] = _gen11_limitations(parent["limitations"])
    state["frozen_invariants"] = _gen11_invariants(parent["frozen_invariants"])
    state["next_milestone"] = _gen11_next_milestone()
    state["test_baseline"] = {**parent["test_baseline"],
                              "milestone": GEN11_MILESTONE,
                              "passed": GEN11_TEST_BASELINE_PASSED,
                              "skipped": GEN11_TEST_BASELINE_SKIPPED,
                              "failed": 0}
    if test_baseline is not None:
        state["test_baseline"] = test_baseline
    return state


def _recompact(limitations: list[str]) -> list[str]:
    """Merge the pairs that state ONE fact at two granularities. Nothing is dropped.

    Each replacement is asserted to carry every clause of both originals by
    ``test_the_recompacted_entries_kept_every_fact_they_merged`` in the S3W.0 suite, on
    the rule S3U applied: a merge that loses a clause is a deletion wearing a merge's
    clothes. Only genuinely duplicate narrative is touched; no machine authority, no
    digest, no count and no invariant is compacted.
    """
    #: Lossless single-entry compressions: filler removed, every clause kept. Asserted
    #: clause-for-clause by ``test_the_recompacted_entries_kept_every_fact_they_merged``.
    compressed = {
        "Hashes binding output_root_id are host-bound": (
            "Hashes binding output_root_id are host-bound: RE-DERIVE on the executing "
            "host, never paste. config_hash and plan_hash also bind runtime and hardware "
            "evidence, so candidate 003's 6f9f470f/414ce9e3 and 004's hold for this "
            "output root only, and a plan hash moves once a run directory exists."),
        "The prospective spend boundary is deliberately EARLIER": (
            "The prospective spend boundary is deliberately EARLIER than proof a forward "
            "pass ran: no atomic transaction spans a durable local append and an external "
            "synchronous call, so a crash in that gap marks spent a holdout no model may "
            "have read. The conservative error is chosen."),
        "receipt_hash and the measurement witness prove": (
            "receipt_hash and the measurement witness prove payload integrity against the "
            "canonical bytes and REPOSITORY PROVENANCE only: not human identity, "
            "authorisation, a signature, who ran it or hardware attestation. Nothing is "
            "signed and no PKI is implied."),
        "A receipt is built AFTER its run": (
            "A receipt is built AFTER its run from artefacts that already exist and needs "
            "the gitignored runtime tree present: it proves what those artefacts say, not "
            "that nobody touched them between run and seal, and cannot be built once the "
            "run directory is gone."),
    }
    merged = {
        # "One run per candidate" and "every S3Q figure is one observation" are one fact.
        "Training and measurement are one host": (
            "One host, CPU, one seed, one run per candidate: no repeat, ablation, second "
            "host, GPU or dtype control arm, and deterministic_reproduction_claimed is "
            "false for candidates 003 and 004 alike. Every S3Q figure is a single "
            "observation with its paired baseline measured in the same run, and no plan "
            "reproduces weights twice."),
        # Candidate 003's gate verdict and its interval are one outcome.
        "Candidate 003 is NOT ELIGIBLE on one deterministic gate": (
            "Candidate 003 is NOT ELIGIBLE on one deterministic gate: schema validity "
            "fell 9/9 -> 8/9, one task, which a 36-task holdout cannot distinguish from "
            "noise. Its paired mean delta is +0.044208, 95% CI [-0.022359, +0.129413]; "
            "the CI does NOT exclude a regression and the run is recorded "
            "regression_not_excluded."),
    }
    absorbed = ("Every S3Q figure is a single observation",
                "Candidate 003's paired mean delta is +0.044208")

    out: list[str] = []
    for entry in limitations:
        if any(entry.startswith(prefix) for prefix in absorbed):
            continue
        for prefix, replacement in {**compressed, **merged}.items():
            if entry.startswith(prefix):
                entry = replacement
                break
        out.append(entry)
    return out


def _recompact_invariants(invariants: list[str]) -> list[str]:
    """The two invariant pairs that state a single rule twice. Every clause is kept."""
    receipt = (
        "An EVALUATED_* state REQUIRES a valid portable receipt and is REDERIVED from "
        "it: a snapshot, verifier constant, prose or human sentence is insufficient in "
        "BOTH directions, and the PRODUCTION decide_eligibility is asked what the "
        "receipt's body-free gate, bootstrap, empirical-status and serialisation "
        "evidence conclude.")
    token = (
        "Plan tokens are single-use and a consumed or failed plan is never replayed. "
        "Token silence is ceremony hygiene, not cryptography: the confirmation string is "
        "a pure function of plan_hash, neither secret nor unpredictable, and PRE-GO "
        "surfaces simply must not materialise it.")
    absorbed = ("An EVALUATED_* state is REDERIVED, never read",
                "Token silence is ceremony hygiene")
    replaced = {"A future EVALUATED_* state requires a valid portable": receipt,
                "Plan tokens are single-use": token}
    out: list[str] = []
    for entry in invariants:
        if entry.startswith(absorbed):
            continue
        for prefix, replacement in replaced.items():
            if entry.startswith(prefix):
                entry = replacement
                break
        out.append(entry)
    return out


def _gen11_limitations(parent: list[str]) -> list[str]:
    """The recompacted set, plus the caveats a qualification session owes its reader.

    The "eligibility is UNKNOWN" fact is EXTENDED in place rather than repeated: an entry
    already says it, and a second one saying the same thing in different words is the
    duplicate narrative this milestone is supposed to be removing, not adding.
    """
    entries = _recompact(parent)
    unknown = "A successful S3V means only that training completed"
    for index, entry in enumerate(entries):
        if entry.startswith(unknown):
            entries[index] = entry.rstrip() + (
                " S3W.0 qualification is readiness, not evidence.")
            break
    else:  # pragma: no cover - the entry is present in every generation from 10
        raise AssertionError("the eligibility-unknown limitation is missing")
    return entries + [
        "The adapter, base weights and runtime were qualified from metadata, digests and "
        "safetensors headers only. Nothing was loaded: what the arms would load is "
        "identified, not proved loadable.",
    ]


def _gen11_invariants(parent: list[str]) -> list[str]:
    return _recompact_invariants(parent) + [
        "GEN11 IS READINESS, NOT AUTHORITY. A qualified ceremony is not an authorised "
        "one: no EVAL capability exists, no human authorised evaluation, no holdout is "
        "spent and no candidate has passed or failed.",
    ]


def _gen11_next_milestone() -> dict:
    """The forward contract, rewritten for S3W.1.

    Four properties are enforced elsewhere and are easy to lose to a compaction, so they
    are called out here: ``primary_axis`` must NAME the dial and BOTH its ends;
    ``ruled_out`` must still bar every subject it is not superseding, in the words the
    sealed S3N.1 suite looks for; ``authority_required`` must name BOTH authorities,
    because a contract mentioning only EVAL reads as though TRAIN were still live; and
    ``holdout_access`` must keep D35, the rule that a spent holdout never decides
    eligibility again. A first draft of this block lost all four and was caught by the
    verifier and that suite. **A forward contract that reads well and bars less is not a
    compaction; it is a weakened invariant.**
    """
    return {
        "name": ("S3W.1 candidate 004 live held-out evaluation on eval-v5, in a NEW "
                 "session, from the FINAL clean generation 11 HEAD. Candidate 004 is "
                 "qualified and unmeasured."),
        "requires_new_session": True,
        "primary_axis": ("MEASURED, not predicted. Candidate 004 against candidate 003: "
                         "learning_rate 1e-4 -> 5e-5, one dial, alpha not slaved so "
                         "alpha/r stays 2.0. Eligibility is UNKNOWN; S3W.0 qualification "
                         "is not evidence of it and training loss never was."),
        "lora_scope": "ATTENTION_AND_MLP",
        "training_corpus": ("m62-defensive-quality-train v2, spent on candidates 003 and "
                            "004, reused UNCHANGED. No train-v3 exists or is proposed; "
                            "S3W.1 trains nothing."),
        "evaluation_holdout": ("m62-defensive-eval v5, FROZEN_UNUSED, spent_by null, "
                               "frozen candidate-blind by S3S before candidate 004 "
                               "existed. eval-v4 is USED_IMMUTABLE and may never be "
                               "reused."),
        "holdout_access": ("eval-v5 task bodies are UNREAD and stay unread: S3W.0 bound "
                           "it by manifest, pack and status only. Its promoted bytes are "
                           "absent from disk, so S3W.1 rebuilds and re-verifies them. "
                           "eval-v4 stays development evidence under D35."),
        "authority_required": [
            "a fresh single-use human EVAL authority, form EVAL:<plan-hash>, bound to a "
            "plan derived from the final generation 11 HEAD, after a token-silent "
            "preflight. The S3V TRAIN authority is SPENT and carries nothing forward",
            "a separate explicit human decision before any promotion, which no authority "
            "here grants",
            "a new explicit human ruling before any axis other than candidate 004's "
            "learning rate moves; every other axis stays barred below",
        ],
        "ruled_out": [
            "evaluating candidate 004 without a fresh single-use EVAL authority at a new "
            "generation, in the session that trained it, under a plan not derived from "
            "the final generation 11 HEAD, or a second time after the durable holdout "
            "commit",
            "any epoch, rank, alpha or dropout change, and any compensating "
            "hyperparameter slaved to the learning rate; a justified second dial is still "
            "a second one",
            "reading candidate 004's learning-rate permission as general. The S3U ruling "
            "superseded the generation-8 learning-rate clause ONLY, for candidate 004 "
            "only, prospectively, and only to 5e-5; a fifth candidate needs its own ruling",
            "a second experimental axis, ATTENTION_ONLY, or any module-surface change; the "
            "LoRA scope stays ATTENTION_AND_MLP over the same seven projections",
            "creating train-v3, adding rows, deleting rows or rebalancing train-v2, a "
            "teacher-generated corpus, adding structured rows or strengthening the "
            "response schema",
            "changing gates, graders, thresholds or the refusal detector, raising "
            "max_new_tokens, changing the seed or reasoning policy, or creating a D38 "
            "gate or a D43 gate",
            "mutating, re-wording, re-scoping, replacing or threshold-tuning frozen "
            "eval-v5, in whole or in part, for any reason including a candidate 004 "
            "result",
            "reusing eval-v4 for any candidate, persisting a raw prompt, target or "
            "response, and reading a task body into any transcript",
            "retraining, resuming, re-seeding or patching candidate 003 or candidate 004 "
            "under its own id, or describing either as approved or production-ready",
            "tuning candidate 004 from its training or validation loss, or treating "
            "either as eligibility evidence",
            "ranking candidates 001-004 in one table; each compares only against its own "
            "simultaneously-measured baseline",
            "back-filling a portable evaluation receipt for candidate 001 or 002",
            "promotion, activation, registry mutation, merge, tag, release or version bump",
            "fixing D39 as a rider",
        ],
    }


# ── generation 12, projected ─────────────────────────────────────────────────────────
def project_gen12(gen11: dict) -> dict:
    """A CONSERVATIVE worst-case post-evaluation state. A shape, never a prediction.

    It assumes the outcome that costs the most bytes to record truthfully: measured,
    NOT eligible, with a blocking gate, a regression that is not excluded and a security
    summary all present at once. An eligible outcome is strictly shorter, so a projection
    that fits this one fits either.
    """
    state = json.loads(json.dumps(gen11))
    state["state_generation"] = gen11["state_generation"] + 1
    state["generation_label"] = "M62_S3W1_CANDIDATE004_EVALUATED_XXXXXXXX"
    state["subject_state_milestone"] = "S3W.1"
    state["subject_state_commit"] = "0" * 40
    state["parent_snapshot_sha256"] = "0" * 64
    state["control_plane_note"] = (
        "S3W.1 evaluated candidate 004 exactly once on eval-v5 under a single-use "
        "plan-bound human EVAL authority, created once and consumed once. The holdout is "
        "USED_IMMUTABLE from its durable model-facing commit onward. 1 eval attempt, 0 "
        "retries. Promotion authority: NONE, and no promotion decision is implied.")

    for candidate in state["candidates"]:
        if candidate["candidate_id"].endswith("-004"):
            candidate["status"] = "EVALUATED_NOT_ELIGIBLE"
            candidate["evaluation_corpus"] = "m62-defensive-eval v5"
            candidate["evaluation_receipt"] = (
                "state/m62/receipts/qwen3-06b-lora-quality-live-004.eval.json")
            candidate["evidence"] = (
                "jarvis/docs/V69_M62_S3W1_CANDIDATE004_LIVE_HELDOUT_EVALUATION.md")
    for dataset in state["datasets"]:
        if dataset["dataset_id"] == "m62-defensive-eval" and dataset["version"] == "v5":
            dataset["status"] = "USED_IMMUTABLE"
            dataset["spent_by"] = (
                "S3W.1 LIVE, candidate 004 (evaluation m62-s3w1-quality-heldout-live "
                "gen-1, plan 00000000, report 00000000)")
            dataset["evidence"] = (
                "jarvis/docs/V69_M62_S3W1_CANDIDATE004_LIVE_HELDOUT_EVALUATION.md")

    state["next_milestone"] = {
        "name": ("S3X candidate 004 postmortem and the fifth-candidate decision, in a "
                 "NEW session. Candidate 004 is measured and terminal; eval-v5 is spent."),
        "requires_new_session": True,
        "primary_axis": ("SEALED. Candidate 004's learning-rate result is measured and "
                         "is not reopened, re-scored or ablated. A fifth candidate needs "
                         "its own operator ruling and its own fresh holdout."),
        "lora_scope": "ATTENTION_AND_MLP",
        "training_corpus": ("m62-defensive-quality-train v2, spent on candidates 003 and "
                            "004. No train-v3 exists and none is proposed."),
        "evaluation_holdout": ("NONE AVAILABLE. eval-v5 is USED_IMMUTABLE, spent_by "
                               "candidate 004. A fifth candidate requires a fresh "
                               "candidate-blind eval-v6 frozen in its own session under "
                               "D35 before any design begins."),
        "holdout_access": ("Every holdout v1-v5 is spent and is development evidence "
                           "under D35. No spent holdout may decide eligibility again, "
                           "and no task body is read into a design session."),
        "authority_required": [
            "a fresh candidate-blind eval-v6 freeze in its own session before any fifth "
            "candidate is designed, and no authority now held carries forward",
            "a separate explicit human decision before any promotion, which no authority "
            "in this repository grants and which candidate 004's result does not imply",
            "a new explicit human ruling before any axis moves for a fifth candidate",
        ],
        "ruled_out": [
            "re-scoring, re-running, ablating or retrying candidate 004 against eval-v5, "
            "which is spent, and reusing any spent holdout for eligibility",
            "reading candidate 004's result as a dose-response curve, a root-cause "
            "confirmation or evidence about any dial other than the one that moved",
            "promotion, activation, registry mutation, merge, tag, release or version "
            "bump on the strength of this or any measured result",
            "designing a fifth candidate in the session that reads eval-v5 bodies, and "
            "shaping eval-v6 around anything candidate 004's per-task results showed",
            "retraining, resuming, re-seeding or patching candidate 003 or candidate 004 "
            "under its own id, and ranking candidates 001-004 in one table",
            "treating training or validation loss as eligibility evidence for any "
            "candidate, and fixing D39 as a rider",
        ],
    }
    state["limitations"] = _gen12_limitations(state["limitations"])
    return state


def _gen12_limitations(gen11: list[str]) -> list[str]:
    """The post-evaluation limitation set.

    Generation 11's three qualification entries are FORWARD-LOOKING: they say eligibility
    is unknown, that eval-v5 has still to be rebuilt, and that no model has been loaded.
    All three are false once the evaluation has run, so a truthful generation 12 REPLACES
    them with what was actually learned rather than accumulating on top of them — and the
    eval-v4/v5 lifecycle entry is rewritten in place for the same reason. That is an
    update of a superseded claim, not a dropped fact: every clause that is still true
    survives into the replacement.
    """
    superseded = (
        "S3W.0 qualified the ceremony body-free and measured NOTHING",
        "eval-v5's promoted bytes are gitignored runtime state, ABSENT",
        "The adapter, base weights and runtime were qualified from metadata",
    )
    lifecycle = "eval-v4 is spent:"
    out: list[str] = []
    for entry in gen11:
        if entry.startswith(superseded):
            continue
        if entry.startswith(lifecycle):
            entry = (
                "eval-v4 and eval-v5 are both spent: no ablation, retry or re-scoring of "
                "candidate 003 or candidate 004 against either is possible in any "
                "circumstance, and a fifth candidate needs a fresh eval-v6 frozen "
                "candidate-blind under D35 before it is designed.")
        out.append(entry)
    return out + [
        "Candidate 004's verdict is a single observation on a 36-task holdout, one host, "
        "one seed, one run, no repeat and no ablation: a one-task move is inside the "
        "resolution this instrument can distinguish from noise.",
        "Candidate 004's result bounds the learning rate at exactly two points in this "
        "lineage and is not a dose-response curve; TRAINING_ROOT_CAUSE_CONFIDENCE stays "
        "NOT_ESTABLISHED whatever the verdict was.",
        "The measured verdict authorises nothing: it is evidence about one adapter on one "
        "spent holdout, and promotion, activation and registry mutation each still "
        "require a separate explicit human decision that no result implies.",
    ]


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
                        help="the commit generation 11 is a claim about")
    parser.add_argument("--emit", default="",
                        help="write the projected generation 11 to this path")
    parser.add_argument("--parent", default="",
                        help="the generation 10 snapshot to project from. Defaults to "
                             "whatever state/m62/current.json points at, which is the "
                             "right answer only until the pointer advances; the digest "
                             "guard below is what actually decides, either way")
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
    if parent["state_generation"] != 10:
        print(f"PARENT_GENERATION_MISMATCH: {parent['state_generation']}")
        return 2

    gen11 = project_gen11(parent, subject_commit=args.subject_commit,
                          parent_sha256=parent_sha)
    gen12 = project_gen12(gen11)
    g11_size, g11_head = measure(gen11)
    g12_size, g12_head = measure(gen12)
    p_bytes, p_head, p_lines, p_line_head = progress_headroom()

    print(f"PARENT_GENERATION: {parent['state_generation']}")
    print(f"PARENT_SNAPSHOT_BYTES: {len(parent_bytes)}")
    print(f"SNAPSHOT_BUDGET_BYTES: {SNAPSHOT_MAX_BYTES}")
    print(f"REQUIRED_HEADROOM_BYTES: {REQUIRED_HEADROOM_BYTES}")
    print(f"PROJECTED_GEN11_SNAPSHOT_BYTES: {g11_size}")
    print(f"PROJECTED_GEN11_HEADROOM_BYTES: {g11_head}")
    print(f"PROJECTED_GEN11_CAPACITY: "
          f"{'PASS' if g11_head >= REQUIRED_HEADROOM_BYTES else 'FAIL'}")
    print(f"PROJECTED_GEN12_SNAPSHOT_BYTES: {g12_size}")
    print(f"PROJECTED_GEN12_HEADROOM_BYTES: {g12_head}")
    print(f"PROJECTED_GEN12_CAPACITY: "
          f"{'PASS' if g12_head >= REQUIRED_HEADROOM_BYTES else 'FAIL'}")
    print(f"PROGRESS_BYTES: {p_bytes} HEADROOM: {p_head}")
    print(f"PROGRESS_LINES: {p_lines} HEADROOM: {p_line_head}")
    print(f"PROGRESS_HEADROOM_INVARIANT: "
          f"{'PASS' if p_head > 0 and p_line_head > 0 else 'FAIL'}")

    ok = (g11_head >= REQUIRED_HEADROOM_BYTES
          and g12_head >= REQUIRED_HEADROOM_BYTES
          and p_head > 0 and p_line_head > 0)

    if args.emit and not ok:
        # Fail closed. A snapshot written past its own gate is a state nobody proved
        # room for, and the next generation is the one that pays for it.
        print("EMIT_REFUSED: the capacity gate did not pass")
        return 1
    if args.emit:
        Path(args.emit).write_bytes(canonical_bytes(gen11))
        print(f"EMITTED: {args.emit}")
        print(f"EMITTED_SHA256: {sha256_bytes(canonical_bytes(gen11))}")

    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
