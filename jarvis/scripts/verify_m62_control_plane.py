#!/usr/bin/env python3
"""scripts/verify_m62_control_plane.py — V69 M62 S3N.1: the control-plane trust boundary.

WHAT THIS IS
------------
An offline, deterministic, read-only verifier for the M62 **Control Plane V2**. It answers
one question, and refuses to answer it optimistically:

    Is the state this repository CLAIMS about M62 consistent with independent evidence?

WHY IT EXISTS
-------------
Before S3N.1 the whole of M62's operational state lived in a 6089-line ``PROGRESS.md``, and
a coding agent's only route to "what is true now" was to read prose and believe it. Two
failure modes follow directly, and both have precedent in this milestone's own defect
ledger: an agent trusts a superseded sentence as current (the D32/D34 shape — a recorded
digest that reproduces nowhere), or an agent reads a *description* of authority as a
*grant* of it.

So the claims moved into a small machine-readable snapshot, and this script cross-checks
the ones that have an independent source:

    branch / ancestry / master        -> Git
    archive integrity                 -> SHA-256 over the bytes
    snapshot chain                    -> SHA-256 over canonical bytes, parent-linked
    policy identities                 -> re-derived from the production classes
    dataset + candidate identities    -> the frozen milestone authority they were sealed in
    state transitions                 -> a closed table, illegal jumps rejected
    authority                         -> NEVER granted here; only observed, and the
                                         observation is measured, not assumed

WHAT IT IS NOT
--------------
It is **not** a security boundary against an attacker who can rewrite the repository. An
adversary able to edit the snapshot, the archive, the tests and this file together defeats
it, and Git — not this script — is the content-addressed history that makes such a rewrite
visible. What it does defeat is the realistic failure: an agent, or a human, changing one
of those things and not the others.

    PROSE_CANNOT_GRANT_AUTHORITY.

Neither ``PROGRESS.md``, nor ``current.json``, nor a snapshot, nor this script's own output
may authorise TRAIN, EVAL, promotion, registry mutation or release. Those remain governed
by the existing single-use plan-token mechanism plus an explicit human decision, and
S3N.1 neither moved nor weakened that mechanism.

GUARANTEES
----------
  * **Offline.** No socket, no name resolution, no HTTP client is imported or used.
  * **No model.** No torch, no transformers, no tokenizer, no weights, no generation.
  * **Read-only.** It opens files for reading and runs read-only ``git`` plumbing. It
    creates, writes, moves and deletes nothing.
  * **Deterministic.** No clock, no randomness, no host identity enters any comparison.
  * **Fail-closed.** An integrity check that cannot be performed is a FAILURE, never a
    silently-skipped pass. Unknown is UNKNOWN; absent evidence is not clean evidence.

Exit codes: ``0`` every check passed, ``1`` at least one problem, ``2`` the control plane
could not be located or parsed at all.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess  # nosec B404 - read-only git plumbing only; argv is a fixed list
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── Repository layout ────────────────────────────────────────────────────────────────
# Resolved from this file's own location, never from the working directory, so the
# verifier answers about the tree it ships in rather than the tree it was invoked from.
_SCRIPTS_DIR = Path(__file__).resolve().parent
_PACKAGE_ROOT = _SCRIPTS_DIR.parent                 # .../jarvis
REPO_ROOT = _PACKAGE_ROOT.parent                    # repository root

CONTROL_PLANE_SCHEMA_VERSION = "m62.control_plane.1"
#: V69 M63 — the CONTENT-ADDRESSED generation format. V2 held every immutable
#: block inline and re-serialised it each generation; at generation 15 that was
#: 28 231 of 33 742 bytes (84%) of pure duplication, leaving 50 bytes of real
#: slack under the policy floor. V3 references those blocks by digest instead.
#:
#: It is a REPRESENTATION change and nothing else. A V3 generation REHYDRATES to
#: the byte-identical V2 document, which :func:`load` does before any check runs
#: — so every check below is written against the V2 shape and is unaware the
#: format changed. The budget is NOT raised; the snapshot simply got smaller.
CONTROL_PLANE_V3_SCHEMA_VERSION = "m62.control_plane.3"
CONTROL_PLANE_SCHEMA_VERSIONS = frozenset({
    CONTROL_PLANE_SCHEMA_VERSION, CONTROL_PLANE_V3_SCHEMA_VERSION})

#: S3P. The schema of the portable, tracked, root-independent receipt that a
#: ``TRAINED_UNEVALUATED`` claim must be backed by. It is a SEPARATE contract from the
#: snapshot on purpose: the snapshot records that a candidate is trained, and the receipt
#: is the independent evidence that it actually is.
TRAIN_RECEIPT_SCHEMA_VERSION = "m62.train_receipt.1"

#: S3Q.0. The portable EVALUATION receipt's version. A candidate may claim an
#: ``EVALUATED_*`` state only when one of these independently establishes it — for the
#: same reason the training receipt exists, one milestone later and one door further in.
EVAL_RECEIPT_SCHEMA_VERSION = "m62.eval_receipt.1"

#: S3Q.0.1. The MODERN portable evaluation receipt. `.1` was qualified against synthetic
#: evidence and, audited before the one-shot live run, was found to leave four holes an
#: auditor could drive a claim through: it emitted EMPTY direct adapter identities, it
#: bound no training receipt, it accepted a caller's candidate label and source commit
#: unchecked, and it copied `eligibility` out of the report so the strongest thing a
#: clean clone could say about an EVALUATED_* claim was "the receipt says so".
#:
#: Those are contract changes, not clarifications, so the version moves rather than `.1`
#: being mutated under the synthetic receipts that already verify against it. No real
#: eval-v4 receipt exists, so nothing is migrated.
EVAL_RECEIPT_V2_SCHEMA_VERSION = "m62.eval_receipt.2"

#: S3Q.0.2. `.2` was qualified against SYNTHETIC evidence and refused the REAL S3Q
#: measurement for three reasons, each reproduced against the live artefacts before a
#: line was written:
#:
#:   * it modelled the paired outcome as an exhaustive three-way `wins/ties/losses`
#:     partition, and production classifies FOUR comparable verdicts -- the fourth,
#:     `security_improvement`, is deliberately not a "win". 11 + 12 + 10 != 36;
#:   * it required the canonical receipt text to encode as ASCII, and a legitimate
#:     production gate message carries U+2212 MINUS SIGN. The report is valid; `.2`
#:     refused its representation;
#:   * it derived `evaluation_source_commit` from the repository HEAD at RECEIPT-BUILD
#:     time, which is only the evaluation source while sealing happens at the unchanged
#:     evaluation commit. After a repair commit that field would name the REPAIR.
#:
#: `.2` is not mutated: it is a tracked, qualified contract whose synthetic receipts
#: already hash against it. The version moves instead.
EVAL_RECEIPT_V3_SCHEMA_VERSION = "m62.eval_receipt.3"

#: The receipt versions a MODERN (non-legacy) EVALUATED_* candidate may present. `.2` is
#: still accepted -- it is a real contract that real evidence can satisfy, and removing it
#: would retroactively invalidate documents written honestly against it. `.1` is not: a
#: `.1` receipt for a run measured after S3Q.0.1 would be evidence deliberately written to
#: a weaker contract, and accepting one would make the upgrade optional.
MODERN_EVAL_RECEIPT_VERSIONS: frozenset[str] = frozenset({
    EVAL_RECEIPT_V2_SCHEMA_VERSION, EVAL_RECEIPT_V3_SCHEMA_VERSION})

#: The receipt versions whose canonical bytes are DEFINED as canonical JSON encoded
#: UTF-8, so non-ASCII production decision text is preserved exactly rather than refused.
#: `.1` and `.2` keep their ASCII-only rule: that rule is part of the contract their
#: existing receipts were hashed under, and relaxing it in place would change what those
#: documents mean.
UTF8_CANONICAL_RECEIPT_VERSIONS: frozenset[str] = frozenset({
    EVAL_RECEIPT_V3_SCHEMA_VERSION})

#: How `.3` states its own encoding, so no "encoding choice" is left open. The digest is
#: SHA-256 over exactly these bytes.
CANONICAL_RECEIPT_ENCODING = (
    "canonical JSON (scripts.verify_m62_control_plane.canonical_json) encoded UTF-8")

#: The production paired-comparison vocabulary, restated here because the schema must
#: build on a host where `training_gym` will not import, and RE-DERIVED from
#: `ComparisonVerdict` by `_check_modern_evaluation_receipt`. A literal nobody re-derives
#: is the second writable copy of a contract.
#:
#: FOUR of these are comparable outcomes of a measured pair. `security_improvement` is
#: its own verdict and is deliberately NOT folded into `improved`: the baseline had a
#: blocking finding the candidate fixed, which is reported and never rewarded as a win.
COMPARISON_VERDICTS: tuple[str, ...] = (
    "security_regression", "security_improvement", "improved", "unchanged",
    "regressed", "not_comparable")

#: The measurement witness `.3` binds its EVALUATION source through. Written before the
#: repair commit, while the repository was still at the evaluated commit; see
#: `check_measurement_witness`.
MEASUREMENT_WITNESS_SCHEMA_VERSION = "m62.measurement_witness.1"
WITNESS_DIR = "state/m62/witnesses"

#: The closed terminal vocabulary, restated here and RE-DERIVED from
#: `EvaluationRunState.is_terminal` by `check_evaluation_receipt`. Restated because the
#: schema must build on a host where the package will not import; re-derived because a
#: literal nobody checks is the second writable copy of a contract.
#:
#: The distinction that matters: `completed` is the only SUCCESSFUL terminal state. A run
#: that ended `failed`, `interrupted` or `quarantined` reached a terminal event and did
#: not reach a measurement.
TERMINAL_EVALUATION_EVENTS: tuple[str, ...] = (
    "completed", "failed", "interrupted", "quarantined")
SUCCESSFUL_TERMINAL_EVALUATION_EVENT = "completed"

#: Candidates whose evaluated state predates the portable evaluation receipt.
#:
#: S3I measured candidate 001 and S3L measured candidate 002, both before this evidence
#: form existed. Their deep milestone documents and sealed artefacts are their authority
#: and they are NOT retrofitted: inventing a receipt for a run that never emitted one
#: would be manufacturing evidence, which is worse than having none. Every candidate
#: outside this set is a MODERN-RECEIPT candidate and must produce one.
#:
#: The set is closed by construction. Nothing may be added to it, because a candidate
#: evaluated after S3Q.0 has the machinery available by definition.
LEGACY_EVALUATION_CANDIDATES: frozenset[str] = frozenset({
    "qwen3-06b-lora-quality-live-001",
    "qwen3-06b-lora-quality-live-002",
})

STATE_DIR = "state/m62"
CURRENT_PATH = f"{STATE_DIR}/current.json"
SNAPSHOT_DIR = f"{STATE_DIR}/snapshots"
#: The V3 content-addressed record store. Each file is named for the sha256 of
#: its own canonical bytes, so a record that is edited stops being findable
#: rather than silently changing what a sealed generation meant.
RECORD_DIR = f"{STATE_DIR}/records"
SCHEMA_DIR = f"{STATE_DIR}/schema"
CURRENT_SCHEMA_PATH = f"{SCHEMA_DIR}/m62-current.schema.json"
SNAPSHOT_SCHEMA_PATH = f"{SCHEMA_DIR}/m62-snapshot.schema.json"
SNAPSHOT_V3_SCHEMA_PATH = f"{SCHEMA_DIR}/m62-snapshot-v3.schema.json"
TRAIN_RECEIPT_SCHEMA_PATH = f"{SCHEMA_DIR}/m62-train-receipt.schema.json"
EVAL_RECEIPT_SCHEMA_PATH = f"{SCHEMA_DIR}/m62-eval-receipt.schema.json"
EVAL_RECEIPT_V2_SCHEMA_PATH = f"{SCHEMA_DIR}/m62-eval-receipt-v2.schema.json"
EVAL_RECEIPT_V3_SCHEMA_PATH = f"{SCHEMA_DIR}/m62-eval-receipt-v3.schema.json"
MEASUREMENT_WITNESS_SCHEMA_PATH = f"{SCHEMA_DIR}/m62-measurement-witness.schema.json"
RECEIPT_DIR = f"{STATE_DIR}/receipts"
MIGRATION_DIR = f"{STATE_DIR}/migrations"
MIGRATION_MANIFEST_PATH = f"{MIGRATION_DIR}/0001-control-plane-v2.json"

PROGRESS_PATH = "PROGRESS.md"
ARCHIVE_PATH = "jarvis/docs/m62/history/PROGRESS_THROUGH_S3N.md"
HISTORY_INDEX_PATH = "jarvis/docs/m62/HISTORY_INDEX.md"
VERIFIER_PATH = "jarvis/scripts/verify_m62_control_plane.py"
MIGRATION_DOC_PATH = "jarvis/docs/V69_M62_S3N1_CONTROL_PLANE_V2_ZERO_TRUST_MIGRATION.md"

#: The files a NORMAL session reads to bootstrap. The historical archive is deliberately
#: absent: reading it is an audit activity, not a bootstrap one.
BOOTSTRAP_SURFACES = (CURRENT_PATH, PROGRESS_PATH)

#: Every surface a routine session may put in front of an agent. The holdout firewall,
#: the secret scan and the private-path scan run over exactly these. The verifier itself
#: is NOT one: it is the checker, and — exactly as S3N's corpus test asserts
#: ``"<think" not in target`` — naming a forbidden token inside the check that forbids it
#: is hygiene, not a leak (operator ruling H4).
SCANNED_SURFACES = (
    PROGRESS_PATH, CURRENT_PATH, HISTORY_INDEX_PATH, MIGRATION_MANIFEST_PATH)

#: Tracked paths whose content can change what a model is, is trained on, or is measured
#: by. A commit touching any of these between ``subject_state_commit`` and HEAD means the
#: recorded state may no longer describe the repository — see ``check_stale_state``.
STATE_BEARING_PRODUCTION = (
    "jarvis/training_gym/",
    "jarvis/scripts/build_evaluation_corpus.py",
    "jarvis/scripts/build_training_corpus.py",
    "jarvis/scripts/build_quality_training_config.py",
    "jarvis/scripts/train_experiment.py",
    "jarvis/scripts/evaluate_adapter.py",
    "jarvis/scripts/training_gym_dataset.py",
    "jarvis/scripts/qualify_reasoning_policy.py",
)

#: Paths that are the control plane itself. Changing these is what a control-plane
#: milestone does, and it is not evidence that the SUBJECT state moved.
CONTROL_PLANE_PATHS = (STATE_DIR + "/", PROGRESS_PATH, VERIFIER_PATH)

#: Immutable historical record. Append-never-edit.
HISTORY_PATHS = ("jarvis/docs/m62/history/",)

#: Deep milestone authority. Read on demand, never rewritten.
MILESTONE_EVIDENCE_PATHS = ("jarvis/docs/", "docs/")

#: Generated at runtime, never tracked (PROGRESS runtime-artifact policy).
RUNTIME_ARTIFACT_ROOTS = (
    "training_gym_datasets/", "training_gym_exports/", "dataset_candidates/",
    "training_runs/", "training_adapters/", "training_checkpoints/",
    "training_quarantine/", "teacher_packets/", "training_gym_artifacts/",
    "evaluation_runs/", "evaluation_artifacts/", "evaluation_reports/",
    "evaluation_quarantine/", "model_candidate_proposals/",
    "jarvis/evaluation/evaluations/", "jarvis/evaluation/reports/",
    "jarvis/evaluation/quarantine/", "jarvis/evaluation/proposals/",
)

#: Files that hold HELD-OUT task BODIES. A control-plane surface may never cite one as
#: an evidence pointer, and no candidate-design session may ever open one. The generator
#: holds every version's material, `v5` -- frozen unspent by S3S -- included.
FORBIDDEN_BODY_SOURCES = (
    "jarvis/scripts/build_evaluation_corpus.py",
    "task-pack.jsonl",
)
#: APPENDED to, never reordered: two sealed test files index this tuple positionally.
FORBIDDEN_BODY_SYMBOLS = ("corpus_v4_material", "corpus_v4(",
                          "corpus_v5_material", "corpus_v5(",
                          "corpus_v6_material", "corpus_v6(")


def body_symbol_version(symbol: str) -> str:
    """The holdout version a body symbol belongs to, so a refusal can name it.

    A refusal that says only "a body source" is harder to act on than one that says which
    exam was about to be published, and the version is already in the symbol.
    """
    match = re.search(r"v[0-9]+", symbol)
    return match.group(0) if match else "held-out"

#: The 36 `eval-v4` task ids, reconstructed from the body-free convention recorded in
#: `V69_M62_S3N_FRESH_EVAL_V4_FREEZE.md` section 4.2. Ids carry no answer content — they
#: are explicitly body-free authority — and they are used here only as a NEGATIVE test:
#: a routine bootstrap surface that names an individual holdout task is carrying
#: per-task material it has no reason to carry.
def _eval_v4_task_ids() -> tuple[str, ...]:
    groups = (
        ("he4-report-", 4), ("he4-evidence-", 4), ("he4-tool-", 2), ("he4-refusal-", 2),
        ("sr4-refusal-", 6), ("sr4-safe-", 6),
        ("adv4-refusal-", 4), ("adv4-report-", 3), ("adv4-evidence-", 3),
        ("adv4-tool-", 2),
    )
    return tuple(f"{stem}{n:02d}" for stem, count in groups for n in range(1, count + 1))


EVAL_V4_TASK_IDS = _eval_v4_task_ids()


#: The 36 `eval-v5` task ids, reconstructed from the body-free convention recorded in
#: `V69_M62_S3S_EVAL_V5_FREEZE.md` section 4.1. Same reasoning as the `v4` set above, and
#: MORE load-bearing: `v5` is FROZEN_UNUSED, so a surface that names one of its tasks is
#: leaking material no model has ever been allowed to read.
def _eval_v5_task_ids() -> tuple[str, ...]:
    groups = (
        ("he5-report-", 4), ("he5-evidence-", 4), ("he5-tool-", 2), ("he5-refusal-", 2),
        ("sr5-refusal-", 6), ("sr5-safe-", 6),
        ("adv5-refusal-", 4), ("adv5-report-", 3), ("adv5-evidence-", 3),
        ("adv5-tool-", 2),
    )
    return tuple(f"{stem}{n:02d}" for stem, count in groups for n in range(1, count + 1))


EVAL_V5_TASK_IDS = _eval_v5_task_ids()


#: The 36 `eval-v6` task ids, from the body-free convention recorded in
#: `V69_M62_S3X1_EVAL_V6_FREEZE.md`. The most load-bearing set of the three: `v6` is
#: FROZEN_UNUSED, fresh, and is the corpus a future candidate-004 evaluation will actually
#: be judged against, so a surface naming one of its tasks leaks the live exam.
def _eval_v6_task_ids() -> tuple[str, ...]:
    groups = (
        ("he6-report-", 4), ("he6-evidence-", 4), ("he6-tool-", 2), ("he6-refusal-", 2),
        ("sr6-refusal-", 6), ("sr6-safe-", 6),
        ("adv6-refusal-", 4), ("adv6-report-", 3), ("adv6-evidence-", 3),
        ("adv6-tool-", 2),
    )
    return tuple(f"{stem}{n:02d}" for stem, count in groups for n in range(1, count + 1))


EVAL_V6_TASK_IDS = _eval_v6_task_ids()

#: `version -> its task ids`. Every holdout whose ids a scanned surface must not name.
#: A version missing from this map is a version no firewall check ever looks for.
HELD_OUT_TASK_IDS: dict[str, tuple[str, ...]] = {
    "v4": EVAL_V4_TASK_IDS,
    "v5": EVAL_V5_TASK_IDS,
    "v6": EVAL_V6_TASK_IDS,
}

# ── Closed vocabularies ──────────────────────────────────────────────────────────────
# Derived from the repository, not invented. `CandidateEligibility` supplies the four
# post-evaluation verdicts; the pre-evaluation spellings are the ones PROGRESS recorded
# for candidates 001-003.

CANDIDATE_STATES = (
    "NOT_CREATED",
    "DESIGNED_UNTRAINED",
    "TRAINED_UNEVALUATED",
    "EVALUATED_NOT_ELIGIBLE",
    "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW",
    "EVALUATED_NEEDS_MORE_EVIDENCE",
    "EVALUATED_QUARANTINED",
    "PROMOTED",
)

#: ``from -> {to: guard}``. A guard names the authority the transition needs; the control
#: plane can supply none of them, which is the point. Any pair absent from this table is
#: ILLEGAL, including every jump that skips training or evaluation.
CANDIDATE_TRANSITIONS: dict[str, dict[str, str]] = {
    "NOT_CREATED": {"DESIGNED_UNTRAINED": "DESIGN_MILESTONE"},
    "DESIGNED_UNTRAINED": {"TRAINED_UNEVALUATED": "TRAIN_AUTHORITY_CONSUMED"},
    "TRAINED_UNEVALUATED": {
        "EVALUATED_NOT_ELIGIBLE": "EVAL_AUTHORITY_CONSUMED",
        "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW": "EVAL_AUTHORITY_CONSUMED",
        "EVALUATED_NEEDS_MORE_EVIDENCE": "EVAL_AUTHORITY_CONSUMED",
        "EVALUATED_QUARANTINED": "EVAL_AUTHORITY_CONSUMED",
    },
    "EVALUATED_NEEDS_MORE_EVIDENCE": {
        "EVALUATED_NOT_ELIGIBLE": "FRESH_EVAL_AUTHORITY_CONSUMED",
        "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW": "FRESH_EVAL_AUTHORITY_CONSUMED",
        "EVALUATED_QUARANTINED": "FRESH_EVAL_AUTHORITY_CONSUMED",
    },
    "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW": {"PROMOTED": "HUMAN_PROMOTION_AUTHORITY"},
    "EVALUATED_NOT_ELIGIBLE": {},
    "EVALUATED_QUARANTINED": {},
    "PROMOTED": {},
}

#: The states a candidate at an ordinal the PARENT generation never mentioned may enter
#: at. Everything else is refused, because a fresh ordinal is the one place a snapshot
#: could otherwise mint a candidate that faces no transition check at all.
#:
#: ``NOT_CREATED`` is the reserved-placeholder route: generation 1 carried ordinal 3 as
#: the literal ``candidate-003`` precisely so that naming it stayed a design decision.
#:
#: ``DESIGNED_UNTRAINED`` was added by S3U, and the reason it is safe is specific rather
#: than convenient: it is the ONLY other state :func:`check_candidate_design`
#: independently RE-DERIVES from the production generator. A snapshot cannot assert it
#: -- the generator must be able to produce that candidate's corpus, base revision,
#: render policy, control and single-axis relation, and the deep evidence must be a
#: tracked file -- so admitting it adds an entry point that is checked harder than the
#: transition it bypasses. Generation 8 recorded no ordinal 4, so this is the entry
#: candidate 004 used.
#:
#: What this deliberately still refuses, and what the guard exists for: a fresh ordinal
#: arriving already ``TRAINED_UNEVALUATED``, ``EVALUATED_*`` or ``PROMOTED``. Those are
#: claims about weights, a spent holdout and a human decision, none of which a design
#: re-derivation can witness. Widening this tuple further is a control-plane migration.
FRESH_ORDINAL_ENTRY_STATES = ("NOT_CREATED", "DESIGNED_UNTRAINED")

#: No promotion authority exists in this repository — ``ModelCandidateProposal`` is
#: non-effectful by construction and mutates no registry. A snapshot that RECORDS a
#: promoted candidate is therefore recording something no repository artefact can
#: witness, and is refused.
UNWITNESSABLE_CANDIDATE_STATES = ("PROMOTED",)

#: Every state whose whole content is "a held-out corpus was spent on this candidate".
#: Derived from the vocabulary rather than restated, so a state added to CANDIDATE_STATES
#: cannot quietly escape the evidence requirement by not appearing in a second list.
EVALUATED_CANDIDATE_STATES: frozenset[str] = frozenset(
    s for s in CANDIDATE_STATES if s.startswith("EVALUATED_"))

#: Two states, and the distinction between them is a scientific property, not a label.
#: ``FROZEN_UNUSED`` means no model has ever read it; ``USED_IMMUTABLE`` means it is
#: spent and its results are design input (the D35 rule). The historical spelling
#: ``FROZEN_UNSEEN`` (S3J/S3K, for eval-v3 before S3L) is the same semantics as
#: ``FROZEN_UNUSED`` and survives only in the archive; it is deliberately not a third
#: current state.
DATASET_STATES = ("FROZEN_UNUSED", "USED_IMMUTABLE")

DATASET_TRANSITIONS: dict[str, dict[str, str]] = {
    # One-way. Relabelling a spent holdout as fresh is the single most damaging edit
    # anyone could make to this file, so it has no edge.
    "FROZEN_UNUSED": {"USED_IMMUTABLE": "EVAL_AUTHORITY_CONSUMED"},
    "USED_IMMUTABLE": {},
}

DATASET_ROLES = ("TRAINING_CORPUS", "EVALUATION_HOLDOUT")

# ── Frozen identities, quoted from the milestone that sealed them ────────────────────
# These are the independent anchors the snapshot is checked AGAINST. They are not a
# second writable copy of the state: the snapshot must agree with them, and both must
# agree with the milestone documents named in each entry's ``evidence`` pointer.
EVAL_V4_PACK_HASH = "95b4e2f6ffb495735113c236f051073449f4562b780eddfc5fe8a7f76bddf2b7"
EVAL_V5_PACK_HASH = "287a9fb61e3feab510763d834f77a75c3a016fe27ba4d04a4ac86c588c09fed6"
EVAL_V6_PACK_HASH = "41579381422636d073d8ce3a0df230cafb97ffdd1489ab02126f2273565ade16"

#: ``"<dataset_id> <version>" -> (status, manifest_hash)``
FROZEN_DATASETS: dict[str, tuple[str, str]] = {
    "m62-defensive-eval v1": (
        "USED_IMMUTABLE",
        "0970600c677c89112db972c6024634aa871be92dee303db7f429c90967d3dd3b"),
    "m62-defensive-eval v2": (
        "USED_IMMUTABLE",
        "82b60bfdbea263eef3990eb6e49c2f2ca16e9b9e26ec8ac435f314b374279d60"),
    "m62-defensive-eval v3": (
        "USED_IMMUTABLE",
        "7c948236163198b5de451316e39346a37efcbc1254724f921e116a6c722f75a0"),
    # S3N froze v4 candidate-blind and S3Q spent it: one plan, one model-facing commit,
    # one terminal event, 72 results. USED_IMMUTABLE from that durable commit onward,
    # whatever happened afterwards -- and what happened afterwards is that the SEAL
    # failed three times and the measurement did not. The transition table has no edge
    # back, deliberately: relabelling a spent holdout as fresh is the single most
    # damaging edit anyone could make to this repository.
    "m62-defensive-eval v4": (
        "USED_IMMUTABLE",
        "8c6871b0094bdfc75062a6352d383fa8e9750c1425182a2b3248db20500081c5"),
    # S3S froze v5 candidate-blind BEFORE candidate 004 existed and spent nothing on it.
    # FROZEN_UNUSED is a scientific claim, not a label: no model has ever read it. The one
    # legal transition out of it is guarded by EVAL_AUTHORITY_CONSUMED, and there is no
    # edge back.
    "m62-defensive-eval v5": (
        "FROZEN_UNUSED",
        "e852f4627d4fe631f58ee3d120d5d1a81c94480a1c0b84e590d2b08261043f4c"),
    # RE-QUOTED AT S3Y, from the milestone that sealed the transition. S3X.1 froze v6
    # candidate-blind as the replacement the v5 ELIGIBILITY retirement requires; S3Y then
    # spent it on candidate 004 under ONE external human EVAL authority, which is the only
    # thing FROZEN_UNUSED -> USED_IMMUTABLE is guarded by (EVAL_AUTHORITY_CONSUMED). The
    # digest is unchanged, there is no edge back, and USED_IMMUTABLE is terminal: no
    # rerun, no second look, and no re-freezing a spent corpus as fresh.
    "m62-defensive-eval v6": (
        "USED_IMMUTABLE",
        "413e675711d51f5b98cb5a8ec7ff7fb0d8eb36b5e4c6dff790fb60f764f8fba6"),
    "m62-defensive-quality-train v1": (
        "USED_IMMUTABLE",
        "9bbac2f057fd0592a30a7fdeb968655f8ea585df00966e1b920415377ab7286a"),
    "m62-defensive-quality-train v2": (
        "USED_IMMUTABLE",
        "24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f"),
}

#: S3V. ``dataset key -> (export manifest, train shard, validation shard)``, the SEALED
#: export identities a training receipt is held to beyond its corpus manifest.
#:
#: THE GAP THIS CLOSES. Through S3P a receipt's corpus binding was its `manifest_hash`
#: alone, so `export_manifest_hash`, `train_shard_hash` and `validation_shard_hash` could
#: each be replaced wholesale and the verifier still passed: the receipt named the right
#: corpus while claiming to have trained on material nothing checked. A candidate's train
#: split is exactly what a reader most needs pinned, because substituting it is how a run
#: silently trains on something other than what it says.
#:
#: These are PINNED rather than re-derived on purpose. The dataset store is a gitignored
#: runtime tree, so a fresh clone cannot rebuild these digests; pinning them is what makes
#: the binding portable. They are content digests of an immutable promoted export, so
#: pinning them dates nothing and a legitimate corpus change is a NEW VERSION with its own
#: row, never an edit to this one.
FROZEN_TRAIN_EXPORTS: dict[str, tuple[str, str, str]] = {
    "m62-defensive-quality-train v2": (
        "82780fa0edc4c99198d0074a8a01b08507fa3eed54b4af50c3e045d5e07ae921",
        "a02797f85d11498103918df9114ed4496e232a9a2c88b738f36f8326a72e1c7e",
        "ae6ffe204df4d2b60b2215aa38a641331cf56d999cc022c24f538fba891bb764"),
}

#: S3V. ``candidate_id -> adapter artifact-set hash``. The adapter's SET digest covers the
#: whole artefact set rather than the weights file alone, so it is the one identity that
#: notices a file appearing or disappearing beside `adapter_model.safetensors`. It cannot
#: be re-derived without the gitignored run tree, which is precisely why the sealed value
#: is recorded here: otherwise a mutated artifact-set hash is accepted by every check.
FROZEN_ADAPTER_ARTIFACT_SETS: dict[str, str] = {
    # S3P's candidate. Recorded here at S3V rather than at S3P because the binding did not
    # exist then; the value is the one its sealed receipt has carried since S3P and is not
    # re-derived from the run tree, which may be gone.
    "qwen3-06b-lora-quality-live-003":
        "148e3ef15e9e3890e25f83ad1b7361192f08ed92c89741a043e4f3985cbf83da",
    "qwen3-06b-lora-quality-live-004":
        "326678618101eb4eec0a12b89a5e02f89340148111d5f4adf97d6a04f449b864",
}

#: ``candidate_id -> (status, adapter_sha256 or None)``
FROZEN_CANDIDATES: dict[str, tuple[str, "str | None"]] = {
    "qwen3-06b-lora-quality-live-001": (
        "EVALUATED_NOT_ELIGIBLE",
        "43213035c15cd38928d2d6a3bdbd9af96872a954801c6bfd0a9b82a8e22ac858"),
    "qwen3-06b-lora-quality-live-002": (
        "EVALUATED_NOT_ELIGIBLE",
        "319c252498ba51e01ed59f58fc20ae639e2d886bf67277d3aa6df2e9f9665409"),
    # S3O named candidate 003 and moved it to DESIGNED_UNTRAINED; S3P spent the one
    # authorised TRAIN capability on it; S3Q spent eval-v4 on it and S3Q.0.2 sealed the
    # result. It is EVALUATED_NOT_ELIGIBLE: measured, and blocked by the frozen canonical
    # eligibility gate. NOT ELIGIBLE IS A RESULT, NOT A FAILURE OF THE RUN -- 72 results,
    # 0 generation errors, 0 security blockers, and one deterministic quality gate that
    # said no.
    #
    # This pair is NOT the evidence for that state, and it is deliberately not enough on
    # its own: `check_candidate_design` re-derives the design from the production
    # generator, `check_training_receipt` refuses the trained claim without a tracked
    # receipt, and `check_evaluation_receipt` refuses the EVALUATED_* claim without a
    # portable one whose verdict it RE-DERIVES. A snapshot agreeing with this constant
    # while any of those disagrees is a FAILURE, not a pass.
    "qwen3-06b-lora-quality-live-003": (
        "EVALUATED_NOT_ELIGIBLE",
        "6ccd8fdc16c6f79d5d7965c1d30a42faecc226581a20f701c582588c76ce4ea6"),
    # S3U designed candidate 004 after an explicit human operator ruling; S3V then spent
    # ONE plan-bound single-use TRAIN authority on it and evaluated nothing. The digest is
    # the adapter S3V actually produced, and it is NOT candidate 003's: a fourth candidate
    # inheriting a third's weights digest is precisely the substitution this pair exists to
    # catch. `check_candidate_design` re-derives the single-axis design from the production
    # generator and `check_training_receipt` re-derives the trained claim from the portable
    # receipt, so this pair agreeing with the snapshot is not on its own a pass.
    # RE-QUOTED AT S3Y. The candidate was measured ONCE against eval-v6 under one human
    # EVAL authority and every deterministic gate passed, so it is
    # EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW -- a REQUEST for a human decision, never one,
    # and never a promotion. eval-v5 remains untouched and unspent: the corpus S3Y spent
    # is v6. The adapter digest is unchanged, because an evaluation measures weights and
    # does not alter them.
    "qwen3-06b-lora-quality-live-004": (
        "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW",
        "a105e01ca99d9b47d45c408a614b78aa9ec22df83ad32b321df57b1a1c3ecc67"),
    # S4B designed candidate 005 after a SECOND explicit human operator ruling, and
    # trained nothing. `None` is the assertion, not a placeholder: a DESIGNED_UNTRAINED
    # candidate that has grown an adapter digest has stopped being designed and started
    # being trained, and this pair is what notices. The value moves exactly once, at the
    # generation that records a training receipt, and `check_training_receipt` re-derives
    # the trained claim from that receipt rather than from this line.
    #
    # Candidate 004's pair above is UNCHANGED and is re-quoted by no one: 005 is a new
    # identity built against 004, never a retry of it. The HOLD on 004 stands.
    "qwen3-06b-lora-quality-live-005": ("DESIGNED_UNTRAINED", None),
}

#: The placeholder identity generation 1 carried, and what S3O resolved it to.
#:
#: Generation 1 recorded ordinal 3 as the literal ``candidate-003`` precisely because
#: naming it was a design decision. Renaming a candidate must not become a way to slip
#: past the transition table, so the parent lookup in :func:`check_candidate_state`
#: resolves by ORDINAL rather than by id; this map records the resolution so the rename
#: is auditable rather than merely tolerated.
CANDIDATE_IDENTITY_RESOLUTIONS: dict[str, str] = {
    "candidate-003": "qwen3-06b-lora-quality-live-003",
}

#: The tracked, root-independent surfaces a DESIGNED_UNTRAINED candidate is re-derived
#: from. `config_hash` is deliberately absent: it binds `output_root_id`, so it is a
#: fact about a filesystem path and reproduces in exactly one clone. Pinning it in the
#: control plane would be pinning this host.
CANDIDATE_003_ID = "qwen3-06b-lora-quality-live-003"
CANDIDATE_003_KEY = "003"
CANDIDATE_CONTROL_KEY = "002"
CANDIDATE_003_EVIDENCE = "jarvis/docs/V69_M62_S3O_CANDIDATE003_CONTROLLED_DESIGN.md"
CANDIDATE_003_LORA_SCOPE = "attention_and_mlp"

#: S3U. Candidate 004: designed, untrained, and the subject of the narrow operator
#: ruling recorded at generation 9. Its axis, its reference and its ruled learning rate
#: are NOT restated here as literals -- they are read from the production generator by
#: `check_candidate_design` and `check_next`, because a verifier constant that agrees
#: with a snapshot while the generator disagrees with both is the circular failure this
#: whole file exists to prevent.
CANDIDATE_004_ID = "qwen3-06b-lora-quality-live-004"
CANDIDATE_004_KEY = "004"
CANDIDATE_004_EVIDENCE = (
    "jarvis/docs/V69_M62_S3U_CANDIDATE004_SINGLE_AXIS_DESIGN.md")
#: The tracked operator-ruling record. It carries a DIGEST of the ruling phrase and not
#: the phrase: a control plane that stored a replayable authorisation string would be
#: minting the capability it is forbidden to hold.
OPERATOR_RULING_S3U = f"{STATE_DIR}/rulings/0001-s3u-candidate004-learning-rate.json"

#: S4B. Candidate 005: designed, untrained, and the subject of a SECOND narrow operator
#: ruling, recorded at generation 17. Like candidate 004's, its axis, its reference and
#: its ruled learning rate are NOT restated here -- they are read from the production
#: generator, because a verifier constant agreeing with a snapshot while the generator
#: disagrees with both is the circular pass this file exists to refuse.
CANDIDATE_005_ID = "qwen3-06b-lora-quality-live-005"
CANDIDATE_005_KEY = "005"
CANDIDATE_005_EVIDENCE = (
    "jarvis/docs/V69_M63_S4B_CANDIDATE005_SINGLE_AXIS_DESIGN.md")
OPERATOR_RULING_S4B = f"{STATE_DIR}/rulings/0002-s4b-candidate005-learning-rate.json"

#: ``candidate id -> (generator key, the tracked ruling its design rests on)``.
#:
#: A MAP rather than a second hard-coded block. S3U checked one ruling by name; a second
#: candidate resting on a second ruling made "the ruling" a category, and a category with
#: one member spelled out in the function body is how the second member goes unchecked.
OPERATOR_RULINGS: dict[str, tuple[str, str]] = {
    CANDIDATE_004_ID: (CANDIDATE_004_KEY, OPERATOR_RULING_S3U),
    CANDIDATE_005_ID: (CANDIDATE_005_KEY, OPERATOR_RULING_S4B),
}

#: S3P. The portable receipt that backs candidate 003's training history.
CANDIDATE_003_TRAIN_RECEIPT = (
    f"{RECEIPT_DIR}/qwen3-06b-lora-quality-live-003.train.json")

#: S3V. The portable receipt that backs candidate 004's TRAINED_UNEVALUATED claim. Named
#: separately from candidate 003's rather than derived from the id, so a receipt pointer
#: that silently swapped to the other candidate's file is a mismatch this file can state.
#: `check_training_receipt` reads the pointer from the SNAPSHOT and re-derives everything
#: from the receipt itself; this constant is the second witness, never the only one.
CANDIDATE_004_TRAIN_RECEIPT = (
    f"{RECEIPT_DIR}/qwen3-06b-lora-quality-live-004.train.json")

#: S3Q.0.2. The portable receipt that backs candidate 003's EVALUATED_NOT_ELIGIBLE claim,
#: and the pre-repair measurement witness it binds its evaluation source through.
CANDIDATE_003_EVAL_RECEIPT = (
    f"{RECEIPT_DIR}/qwen3-06b-lora-quality-live-003.eval.json")
S3Q_MEASUREMENT_WITNESS = (
    f"{STATE_DIR}/witnesses/0001-s3q-live-measurement-witness.json")

#: The evaluation that spent eval-v4, named once so no surface can invent a second one.
S3Q_EVALUATION_ID = "m62-s3q-quality-heldout-live"
S3Q_EVALUATION_GENERATION = 1
S3Q_PLAN_HASH = (
    "5ef8735337e6244293b44a735e699c8f04174eb331cc236b862f548ef3e9cfbb")
S3Q_REPORT_HASH = (
    "bf7dd00d06396a6d8838b8309afced61c0f3be7e98945f0cd81c2d52a46123f1")

#: The commit the measurement ran at. NOT the commit that built the receipt -- the whole
#: reason `m62.eval_receipt.3` exists is that those are two different things (D42).
S3Q_EVALUATION_SOURCE_COMMIT = "c2c025e720e9c3e595c45ca32bd96bbe974f548e"

#: The STRUCTURE a candidate-002-architecture adapter must have, quoted from the sealed
#: S3K live-training record (§10.3) — the milestone that measured it on real weights.
#:
#: Candidates 002 and 003 share rank 16 over the same seven projections across the same
#: 28 layers, so their adapters must be structurally IDENTICAL: 28 x 7 x 2 = 392 tensors,
#: half ``lora_A`` and half ``lora_B``, over the same parameter counts. Learned VALUES
#: differ, and must -- that is the experiment. STRUCTURE differing would mean the run did
#: not adapt what the design said it would, and is refused rather than reinterpreted.
STRUCTURAL_ADAPTER_CONTROL: dict[str, int] = {
    "lora_tensor_count": 392,
    "lora_a_tensors": 196,
    "lora_b_tensors": 196,
    "non_lora_tensors": 0,
    "trainable_parameters": 10_092_544,
    "total_parameters": 606_142_464,
}

#: The seven Qwen3 linear projections ``ATTENTION_AND_MLP`` resolves to, ordered as the
#: production LoRA policy emits them. Compared as a SET against the receipt, because the
#: adapter records the modules it was asked to adapt, not a canonical ordering.
STRUCTURAL_ADAPTER_TARGET_MODULES = (
    "q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj")

#: ``defect id -> status``, as the milestone that closed or opened it recorded.
FROZEN_DEFECT_STATUSES: dict[str, str] = {
    "D37": "FIXED",
    "D38": "FIXED_OBSERVABILITY_ONLY",
    "D39": "OPEN",
    # S3Q.0.2. The three ways `m62.eval_receipt.2` refused a measurement that was
    # correct. All three were defects in the RECEIPT, and all three were closed by
    # moving the contract to the evidence -- never the evidence to the contract.
    "D40": "FIXED",
    "D41": "FIXED",
    "D42": "FIXED",
    # S3T.0. Observability, PROSPECTIVE, and emphatically not a gate. Pinned here so a
    # later snapshot cannot quietly re-record it as one.
    "D43": "FIXED_OBSERVABILITY_ONLY",
}

DEFECT_STATES = (
    "OPEN", "FIXED", "FIXED_OBSERVABILITY_ONLY", "ACCEPTED_KNOWN_LIMITATION",
    "NOT_QUALIFIED", "SUPERSEDED", "OPERATOR_RULING",
)

#: Tri-state. ``UNKNOWN`` is a real value and never collapses to a clean reading; that is
#: the D38 lesson written into the schema.
AUTHORITY_OBSERVATIONS = ("NONE_OBSERVED_IN_REPOSITORY", "UNKNOWN")

# ── Things a control-plane document may never contain ────────────────────────────────
#: A key whose name reads as a grant. The control plane observes; it does not authorise.
FORBIDDEN_AUTHORITY_KEYS = (
    "train_authority", "eval_authority", "authority", "authorized", "authorised",
    "authorization", "authorisation", "grant", "granted", "capability", "token",
    "train_token", "eval_token", "promotion_authority", "permit", "allow_train",
    "allow_eval", "may_train", "may_eval",
)

#: A value shaped like a spendable plan token. No tracked file may hold one, by the
#: PROGRESS runtime-artifact invariant; here it is measured rather than assumed.
#:
#: ONE pattern string, two consumers: Python's ``re`` and ``git grep -E``. Writing it
#: twice is how the in-process scan and the tree-wide scan quietly stop meaning the same
#: thing, so it is deliberately restricted to the POSIX ERE both accept — no ``\b``, no
#: non-capturing group. Without word anchors it matches strictly more, which is the
#: fail-safe direction for a scan whose job is to refuse.
TOKEN_LITERAL_PATTERN = r"(TRAIN|EVAL|PROMOTE):[0-9a-f]{16,64}"
TOKEN_LITERAL_RE = re.compile(TOKEN_LITERAL_PATTERN)

#: Prose shaped like a grant — ``TRAIN authorized: true`` and its relatives. Finding one
#: is reported as an AMBIGUITY, not as a failure and emphatically not as a capability:
#: a sentence cannot authorise anything here, and treating it as suspicious rather than
#: as broken is the honest reading. The pattern requires an affirmative boolean so that
#: ordinary prose about what authority *would* be needed does not trip it.
AUTHORITY_CLAIM_RE = re.compile(
    r"(TRAIN|EVAL|PROMOTION|PROMOTE)\b[^\n]{0,24}"
    r"(authoriz|authoris|approved|granted|permitted)[^\n]{0,12}"
    r"[:=]?\s*\b(true|yes|1)\b",
    re.IGNORECASE)

#: A key that could hold task material.
FORBIDDEN_BODY_KEYS = (
    "prompt", "user_prompt", "system_prompt", "target", "expected_answer",
    "hidden_target", "task_body", "material", "response", "body", "answer", "text",
)

#: Longest non-digest string any control-plane JSON value may carry. Long free text is
#: how a body arrives in instalments (the D27 shape).
MAX_JSON_STRING_CHARS = 320

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
PRIVATE_PATH_RE = re.compile(r"(/home/[A-Za-z0-9._-]+|/Users/[A-Za-z0-9._-]+|[A-Za-z]:\\\\?Users)")

# ── Size budgets ─────────────────────────────────────────────────────────────────────
# Derived in S3N.1 from the migrated sizes with headroom, per the rule
# ``max(current * 1.5, current + 150)`` capped by a reviewed absolute ceiling. Raising
# one of these is an explicit control-plane migration decision, never a side effect of a
# normal milestone.
# PROGRESS.md migrated at 510 lines / 27,013 bytes.
#   lines: max(510 * 1.5, 510 + 150) = 765  -> 760, about 250 lines of headroom
#   bytes: max(27013 * 1.5, 27013 + 8000)   -> 40,960 (40 KiB)
PROGRESS_MAX_LINES = 760
PROGRESS_MAX_BYTES = 40_960
# Snapshot migrated at 19,141 bytes: max(x * 1.5, x + 8000) -> 32,768.
# S3X.0 MIGRATED 32,768 -> 34,816 (34 KiB) under an explicit recorded operator ruling.
# WHY, and why this number: a truthful recovery generation 12 must ADD the D44 incident,
# the ruling, the defect and the retirement invariants while REPLACING nothing, because
# candidate 004 is still unmeasured and every forward-looking entry stays live. The
# leanest truthful projection measured 32,739 bytes -- 29 bytes of headroom under the old
# cap, against a required 1,024. Recompaction was attempted first and made the block
# LARGER (-133 bytes), and no supported mechanism archives ``limitations`` or ``defects``,
# so the only alternatives were raising a reviewed budget or deleting recorded authority.
# +2,048 bytes is the smallest step that clears the policy headroom without inviting a
# second migration. This is still a STRICT FINITE BUDGET, not a relaxation: the invariant
# remains ``snapshot_size <= SNAPSHOT_MAX_BYTES`` with >= REQUIRED_HEADROOM_BYTES spare.
SNAPSHOT_MAX_BYTES = 34_816
# current.json migrated at 398 bytes. A pointer that needs a kilobyte is a second state.
CURRENT_MAX_BYTES = 2_048
# DELIBERATELY still 32 KiB. It shared a value with SNAPSHOT_MAX_BYTES by coincidence,
# never by derivation; the S3X.0 ruling migrated the snapshot budget alone.
HISTORY_INDEX_MAX_BYTES = 32_768


# ── Canonical serialization ──────────────────────────────────────────────────────────
def canonical_json(payload: object) -> str:
    """The ONE serialization every control-plane digest is taken over.

    UTF-8, keys sorted, two-space indent, no NaN/Infinity, exactly one trailing newline.
    ``ensure_ascii=False`` is safe because a separate check refuses non-ASCII in these
    files, and it keeps the bytes on disk equal to the bytes that are hashed — so a
    snapshot's digest is the digest of the file you can read, with no re-serialization
    step in between that could disagree.

    There is exactly one implementation. Production and tests both call this one.
    """
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2,
                      allow_nan=False) + "\n"


def canonical_bytes(payload: object) -> bytes:
    return canonical_json(payload).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


# ── A strict validator over the JSON Schema subset used here ─────────────────────────
_SUPPORTED_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "enum", "const",
    "pattern", "minLength", "maxLength", "minimum", "maximum", "items", "minItems",
    "maxItems", "uniqueItems", "description", "$schema", "$id", "title", "oneOf",
})


def validate_against_schema(schema: dict, value: object, *, path: str = "$") -> list[str]:
    """Validate *value* against the JSON Schema subset in *schema*.

    This is a real second opinion, not a convenience wrapper: it runs with nothing but the
    standard library, so the control plane still fails closed on a host where the
    ``jsonschema`` package is absent. When that package IS importable the verifier runs it
    too and requires the two to agree, which is what stops this implementation from
    quietly drifting into a weaker reading of the same document.
    """
    problems: list[str] = []
    _validate_node(schema, value, path, problems)
    return problems


def _validate_node(schema: dict, value: object, path: str, problems: list[str]) -> None:
    unknown = set(schema) - _SUPPORTED_KEYWORDS
    if unknown:  # a keyword this validator does not implement must not read as satisfied
        problems.append(f"{path}: schema uses unsupported keyword(s) {sorted(unknown)}")
        return

    if "oneOf" in schema:
        matches = [s for s in schema["oneOf"] if not validate_against_schema(s, value, path=path)]
        if len(matches) != 1:
            problems.append(f"{path}: matched {len(matches)} of {len(schema['oneOf'])} "
                            f"oneOf branches, exactly 1 required")
        return

    expected = schema.get("type")
    if expected is not None and not _type_ok(expected, value):
        problems.append(f"{path}: expected type {expected}, got {_type_name(value)}")
        return

    if "const" in schema and value != schema["const"]:
        problems.append(f"{path}: must be {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: {value!r} is not one of {schema['enum']}")

    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            problems.append(f"{path}: does not match {schema['pattern']!r}")
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{path}: longer than {schema['maxLength']}")

    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: {value} below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: {value} above maximum {schema['maximum']}")

    if isinstance(value, dict):
        for key in schema.get("required", ()):
            if key not in value:
                problems.append(f"{path}: missing required key {key!r}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in sorted(set(value) - set(props)):
                problems.append(f"{path}: unknown key {key!r} is refused")
        for key, sub in props.items():
            if key in value:
                _validate_node(sub, value[key], f"{path}.{key}", problems)

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{path}: {len(value)} items, minimum {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{path}: {len(value)} items, maximum {schema['maxItems']}")
        if schema.get("uniqueItems") and len(
                {canonical_json(i) for i in value}) != len(value):
            problems.append(f"{path}: duplicated item")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate_node(schema["items"], item, f"{path}[{index}]", problems)


def _type_ok(expected: str, value: object) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        # S3P. A loss is a float and an epoch count can be either. JSON Schema's
        # `number` admits both, and excludes `bool` for the same reason `integer` does:
        # `True` is an int in Python and is not a measurement anywhere.
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _type_name(value: object) -> str:
    for name in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _type_ok(name, value):
            return name
    return type(value).__name__


# ── The schemas ──────────────────────────────────────────────────────────────────────
_SHA256 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_COMMIT = {"type": "string", "pattern": "^[0-9a-f]{40}$"}
_SHORT = {"type": "string", "minLength": 1, "maxLength": MAX_JSON_STRING_CHARS}
_REPO_PATH = {"type": "string", "minLength": 1, "maxLength": 200,
              "pattern": "^[A-Za-z0-9][A-Za-z0-9._/-]*$"}


def _obj(properties: dict, *, required: "list[str] | None" = None,
         description: str = "") -> dict:
    node = {"type": "object", "additionalProperties": False, "properties": properties,
            "required": sorted(required if required is not None else properties)}
    if description:
        node["description"] = description
    return node


def current_schema() -> dict:
    """The pointer schema. Small on purpose: a pointer that grows becomes a second state."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "m62-current.schema.json",
        "title": "M62 control-plane current pointer",
        "description": (
            "A deterministic pointer to the latest verified state snapshot. It carries no "
            "prose, no history, no task material and no authority. It cannot grant TRAIN, "
            "EVAL, promotion, registry mutation or release."),
        **_obj({
            "schema_version": {"enum": sorted(CONTROL_PLANE_SCHEMA_VERSIONS)},
            "state_generation": {"type": "integer", "minimum": 1, "maximum": 100_000},
            "latest_snapshot_path": _REPO_PATH,
            "latest_snapshot_sha256": _SHA256,
            "subject_state_commit": _COMMIT,
            "verify_command": _SHORT,
        }),
    }


def train_receipt_schema() -> dict:
    """S3P — the portable training receipt's contract.

    Strict and closed, like the other two. What it may NOT contain matters as much as
    what it must: no token literal, no absolute path, no dataset row, no model output and
    no ``eval-v4`` material. Those are enforced by :func:`check_training_receipt`
    scanning the bytes, because a schema can require a field to be absent but cannot see
    a secret smuggled inside a permitted string.

    It carries **no timestamp and no self-referential digest**. The receipt's identity is
    its bytes; the snapshot that points at it is what records the digest, so the two can
    never agree with each other by construction.
    """
    counted = {"type": "integer", "minimum": 0, "maximum": 10_000_000_000}
    loss = {"type": "number"}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "m62-train-receipt.schema.json",
        "title": "M62 portable training receipt",
        "description": (
            "Root-independent, tracked evidence that one candidate completed one "
            "authorised training run. It is EVIDENCE, not an authority: it cannot grant "
            "TRAIN, EVAL, promotion, registry mutation or release, it holds no token "
            "literal, and it holds no task material. A future clean clone uses it to "
            "establish the training history without the gitignored runtime tree."),
        **_obj({
            "schema_version": {"const": TRAIN_RECEIPT_SCHEMA_VERSION},
            "candidate_id": _SHORT,
            "design_milestone": {"type": "string", "pattern": "^S[0-9A-Z.]{1,10}$"},
            "training_milestone": {"type": "string", "pattern": "^S[0-9A-Z.]{1,10}$"},
            "design_commit": _COMMIT,
            "training_source_commit": _COMMIT,
            "plan_hash": _SHA256,
            "training_config_hash": _SHA256,
            "training_config_hash_is_root_bound": {"const": True},
            "base_model": _obj({
                "model_id": _SHORT, "revision": _COMMIT,
                "identity_hash": _SHA256,
                "tokenizer_id": _SHORT, "tokenizer_revision": _COMMIT,
                "chat_template_digest": _SHA256,
            }),
            "training_dataset": _obj({
                "dataset_id": _SHORT,
                "version": {"type": "string", "pattern": "^v[0-9]+$"},
                "manifest_hash": _SHA256, "reference_hash": _SHA256,
                "export_manifest_hash": _SHA256,
                "train_shard_hash": _SHA256, "validation_shard_hash": _SHA256,
                "hidden_evaluation_hash": _SHA256, "security_regression_hash": _SHA256,
            }),
            "representation": _obj({
                "reasoning_policy": {"enum": ["disabled", "enabled", "model_default"]},
                "chat_render_policy_hash": _SHA256,
                "full_sequence_render_policy_hash": _SHA256,
                "assistant_only_loss": {"const": True},
                "masking_strategy": _SHORT,
            }),
            "authority": _obj({
                # The FORM, never an instance. A receipt that carried a spendable string
                # would be handing a reader the capability instead of the evidence.
                "form": {"const": "TRAIN:<plan-hash>"},
                "bound_plan_hash": _SHA256,
                "creations": counted, "consumptions": counted,
                "token_literal_recorded": {"const": False},
                "retry_authorized": {"const": False},
            }),
            "execution": _obj({
                "terminal_status": {"type": "string", "pattern": "^[A-Z_]{3,32}$"},
                "run_state": _SHORT, "backend_status": _SHORT,
                "completed": {"type": "boolean"},
                "interrupted": {"type": "boolean"},
                "error_category": _SHORT,
                "optimizer_steps_planned": counted,
                "optimizer_steps_requested": counted,
                "optimizer_steps_completed": counted,
                "epochs_configured": counted,
                "epochs_completed": {"type": "number"},
                "seed": counted,
                "converted_records": counted, "truncated_records": counted,
                "train_loss": loss,
                "validation_evaluations": counted,
                "validation_losses": {"type": "array", "maxItems": 64, "items": loss},
                "final_validation_loss": {"oneOf": [{"type": "null"}, loss]},
                "final_validation_present": {"type": "boolean"},
                "validation_rows": counted,
                "validation_contributes_gradients": {"const": False},
                "validation_is_held_out_eligibility_evidence": {"const": False},
                "early_stopping": {"const": False},
                "load_best_model_at_end": {"const": False},
                "generation_performed": {"const": False},
                "backend_warnings": {"type": "array", "maxItems": 32, "items": _SHORT},
            }),
            "adapter": _obj({
                "sha256": _SHA256, "manifest_hash": _SHA256,
                "artifact_set_hash": _SHA256,
                "bytes": counted, "total_bytes": counted,
                "file_names": {"type": "array", "minItems": 1, "maxItems": 32,
                               "items": _SHORT},
                "lora_tensor_count": counted,
                "lora_a_tensors": counted, "lora_b_tensors": counted,
                "non_lora_tensors": counted,
                "adapter_parameter_count": counted,
                "trainable_parameters": counted, "total_parameters": counted,
                "target_modules": {"type": "array", "minItems": 1, "maxItems": 32,
                                   "items": _SHORT},
                "lora_rank": counted, "lora_alpha": counted,
                "lora_dropout": {"type": "number"},
                "dtypes": {"type": "array", "minItems": 1, "maxItems": 8,
                           "items": _SHORT},
            }),
            "verification": _obj({
                "completed_run_verifier": {"enum": ["PASS", "FAIL"]},
                "completed_run_problems": {"type": "array", "maxItems": 64,
                                           "items": _SHORT},
                "adapter_verifier": _SHORT,
                "adapter_problems": {"type": "array", "maxItems": 64, "items": _SHORT},
                "checkpoint_directories": counted,
                "nested_directories": counted,
                "symlinks": counted,
            }),
            "runtime": _obj({
                "device_category": _SHORT, "precision": _SHORT,
                "package_versions": {"type": "object"},
                "local_files_only": {"const": True},
                "trust_remote_code": {"const": False},
                "deterministic_reproduction_claimed": {"type": "boolean"},
            }),
            "runtime_evidence_digest": _SHA256,
            "model_cache_evidence": _SHA256,
            "ledger": _obj({
                "events": {"type": "object"},
                "plan_hashes": {"type": "array", "minItems": 1, "maxItems": 8,
                                "items": _SHA256},
            }),
            "holdout": _obj({
                "evaluation_corpus": {"type": "null"},
                "held_out_evaluation_runs": {"const": 0},
                "eval_authority_created": {"const": False},
                "model_response_tokens_generated": {"const": 0},
            }),
        }),
    }


def eval_receipt_schema() -> dict:
    """S3Q.0 — the portable EVALUATION receipt's contract. Strict and closed.

    What it may NOT contain matters as much as what it must: no held-out prompt, no
    target, no model response, no confirmation literal and no absolute path. A schema can
    require a field to be absent but cannot see material smuggled inside a permitted
    string, so :func:`check_evaluation_receipt` scans the bytes as well.

    ``receipt_hash`` is the digest of the payload with that one field removed. Self-
    checking without being self-referential: it can be re-derived by anyone holding the
    bytes, and it moves the moment any bound fact does.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "m62-eval-receipt.schema.json",
        "title": "M62 portable evaluation receipt",
        "description": (
            "Root-independent, tracked evidence that ONE candidate completed ONE held-out "
            "evaluation under ONE spent single-use authority. It is EVIDENCE OF AN "
            "OPERATION and never AUTHORITY FOR ANOTHER: it grants no retry, no second "
            "evaluation, no promotion, no activation, no registry mutation and no "
            "release. It carries no task body, no target, no model response and no "
            "confirmation literal."),
        **_obj({
            "schema_version": {"const": EVAL_RECEIPT_SCHEMA_VERSION},
            "receipt_version": {"const": EVAL_RECEIPT_SCHEMA_VERSION},
            "receipt_hash": _SHA256,
            "evaluation_milestone": {"type": "string", "pattern": "^S[0-9A-Z.]{1,10}$"},
            "evaluation_source_commit": _COMMIT,
            "evaluation_id": _SHORT,
            "evaluation_generation": {"type": "integer", "minimum": 1,
                                      "maximum": 100_000},
            "candidate": _obj({
                "candidate_id": _SHORT,
                "status_claim": {"enum": sorted(EVALUATED_CANDIDATE_STATES)},
                "adapter_reference_hash": _SHA256,
                "adapter_sha256": {"oneOf": [{"const": ""}, _SHA256]},
                "adapter_manifest_hash": {"oneOf": [{"const": ""}, _SHA256]},
            }),
            "baseline": _obj({
                "reference_hash": _SHA256,
                "tokenizer_identity_hash": _SHA256,
            }),
            "holdout": _obj({
                "dataset_id": _SHORT,
                "dataset_version": {"type": "string", "pattern": "^v[0-9]+$"},
                "dataset_manifest_hash": _SHA256,
                "task_pack_hash": _SHA256,
                "hidden_target_store_hash": _SHA256,
                "pack_manifest_shard_hashes": {"type": "object"},
                "split_manifest_hashes": {"type": "object"},
                "task_count": {"type": "integer", "minimum": 1, "maximum": 100_000},
                "counts_by_split": {"type": "object"},
                "counts_by_family": {"type": "object"},
                "counts_by_kind": {"type": "object"},
                "spent_by_this_evaluation": {"const": True},
            }),
            "plan": _obj({
                "plan_hash": _SHA256,
                "plan_schema_version": _SHORT,
                "evaluator_version": _SHORT,
                "order_policy": _SHORT,
                "order_assignment_hash": _SHA256,
                "performs_inference": {"type": "boolean"},
                "binds_exact_pack_identity": {"const": True},
            }),
            "policies": _obj({
                "generation_policy_hash": _SHA256,
                "grader_policy_hash": _SHA256,
                "metric_policy_hash": _SHA256,
                "statistical_policy_hash": _SHA256,
                "gate_policy_hash": _SHA256,
                "family_policy_hash": _SHA256,
                "dependency_report_hash": _SHA256,
                "hardware_report_hash": _SHA256,
            }),
            "authority": _obj({
                "form": {"const": "EVAL:<plan-hash>"},
                "bound_plan_hash": _SHA256,
                "creations": {"type": "integer", "minimum": 1, "maximum": 1},
                "consumptions": {"type": "integer", "minimum": 1, "maximum": 1},
                "token_literal_recorded": {"const": False},
                "retry_authorized": {"const": False},
                "grants_no_further_authority": {"const": True},
            }),
            "ledger": _obj({
                "plan_started_count": {"type": "integer", "minimum": 1, "maximum": 1},
                "holdout_commit_count": {"type": "integer", "minimum": 1, "maximum": 1},
                "terminal_count": {"type": "integer", "minimum": 1, "maximum": 1},
                "terminal_event": _SHORT,
                "events": {"type": "object"},
                "plan_hashes": {"type": "array", "minItems": 1, "maxItems": 8,
                                "items": _SHA256},
            }),
            "holdout_commit": _obj({
                "commit_schema_version": _SHORT,
                "pack_identity_hash": _SHA256,
                "order_assignment_hash": _SHA256,
                "first_task_id": _SHORT,
                "first_task_hash": _SHA256,
                "first_arm": {"enum": ["baseline", "candidate"]},
                "first_request_parity_hash": _SHA256,
                "task_count": {"type": "integer", "minimum": 1, "maximum": 100_000},
                "backend_id": _SHORT,
            }),
            "execution": _obj({
                "run_state": _SHORT,
                "empirical_status": _SHORT,
                "backend_ids": {"type": "array", "minItems": 1, "maxItems": 8,
                                "items": _SHORT},
                "backend_version": {"type": "string", "maxLength": 200},
                "task_count": {"type": "integer", "minimum": 1, "maximum": 100_000},
                "measured_pairs": {"type": "integer", "minimum": 0, "maximum": 100_000},
                "missing_pairs": {"type": "integer", "minimum": 0, "maximum": 100_000},
                "wins": {"type": "integer", "minimum": 0, "maximum": 100_000},
                "ties": {"type": "integer", "minimum": 0, "maximum": 100_000},
                "losses": {"type": "integer", "minimum": 0, "maximum": 100_000},
                "artifact_verification": {"const": "PASS"},
                "artifact_problems": {"type": "array", "maxItems": 0},
            }),
            "evidence": _obj({
                "report_hash": _SHA256,
                "evaluation_manifest_hash": _SHA256,
                "artifact_tree_hash": _SHA256,
                "comparison_manifest_hash": _SHA256,
                "metrics_summary_hash": _SHA256,
                "pack_manifest_hash": _SHA256,
                "files": {"type": "object"},
            }),
            "outcome": _obj({
                "eligibility": _SHORT,
                "human_review_required": {"type": "boolean"},
                "promotes_model": {"const": False},
                "activates_model": {"const": False},
                "mutates_model_registry": {"const": False},
                "gate_blockers": {"type": "array", "maxItems": 64, "items": _SHORT},
                "limitations": {"type": "array", "maxItems": 64, "items": _SHORT},
            }),
        }),
    }


#: Gate prose, a bootstrap claim and a decision rationale are machine-generated from
#: counts, thresholds and policy names by `gates.py`, `statistics.py` and `reports.py` --
#: none of which is ever handed a prompt, a held-out target or a model response. They run
#: longer than `MAX_JSON_STRING_CHARS`, so they get their own bound rather than the
#: 320-character one that exists to stop free text arriving in instalments. The bound is
#: still a bound: it is what keeps "the decision evidence" from becoming "a place to put
#: a paragraph".
_DECISION_TEXT = {"type": "string", "minLength": 1, "maxLength": 1000}
_MEASURED = {"oneOf": [{"type": "number"}, _SHORT, {"type": "null"}]}
_COUNT = {"type": "integer", "minimum": 0, "maximum": 100_000}


def _gate_evidence_schema() -> dict:
    """One serialised `GateReport`. Strict, because a partial one decides nothing."""
    finding = _obj({
        "gate": _SHORT,
        "kind": {"enum": ["security", "quality", "family", "coverage", "statistical",
                          "operational"]},
        "severity": {"enum": ["blocking", "warning", "info"]},
        "message": _DECISION_TEXT,
        "observed": _MEASURED,
        "threshold": _MEASURED,
        "threshold_calibrated": {"type": "boolean"},
    })
    return _obj({
        "gates_version": _SHORT,
        "passed": {"type": "boolean"},
        "blocking_count": _COUNT,
        "security_blocking_count": _COUNT,
        "warning_count": _COUNT,
        "findings": {"type": "array", "maxItems": 128, "items": finding},
        "evaluated_gates": {"type": "array", "maxItems": 128, "items": _SHORT},
        "security_is_a_veto_not_a_weight": {"const": True},
        "thresholds_are_calibrated": {"type": "boolean"},
        "limitations": {"type": "array", "maxItems": 64, "items": _DECISION_TEXT},
    })


def _bootstrap_evidence_schema() -> dict:
    """One serialised `BootstrapReport`, including the derived claims it must reproduce."""
    return _obj({
        "statistics_version": _SHORT,
        "verdict": {"enum": ["sufficient", "small_sample", "insufficient_evidence",
                             "invalid"]},
        "n_pairs": _COUNT, "n_excluded": _COUNT, "n_missing": _COUNT,
        "mean_delta": {"type": "number"}, "median_delta": {"type": "number"},
        "wins": _COUNT, "ties": _COUNT, "losses": _COUNT,
        "ci_low": {"type": "number"}, "ci_high": {"type": "number"},
        "confidence_level": {"type": "number", "minimum": 0, "maximum": 1},
        "iterations": {"type": "integer", "minimum": 0, "maximum": 10_000_000},
        "seed": {"type": "integer", "minimum": 0, "maximum": 2**32 - 1},
        "method": _SHORT,
        "regression_margin": {"type": "number"},
        "error_accounting": _SHORT,
        "observed_improvement": {"type": "boolean"},
        "excludes_regression_margin": {"type": "boolean"},
        "indicates_regression": {"type": "boolean"},
        "claim": _DECISION_TEXT,
        "p_value_reported": {"const": False},
        "limitations": {"type": "array", "maxItems": 64, "items": _DECISION_TEXT},
    })


def eval_receipt_v2_schema() -> dict:
    """S3Q.0.1 -- the MODERN portable evaluation receipt's contract.

    WHAT `.2` ADDS, AND WHY EACH ADDITION IS A REFUSAL RATHER THAN A FIELD
    ---------------------------------------------------------------------
    * DIRECT ADAPTER IDENTITY. `.1` wrote `adapter_sha256: ""` and
      `adapter_manifest_hash: ""` and its schema PERMITTED the empty string, so the one
      question a later auditor most needs answered -- which weights were measured --
      had a blank where the answer belongs. Here all four adapter identities are
      64-hex-mandatory and an empty string is a schema violation.
    * TRAINING-RECEIPT BINDING. The evaluated candidate is now rooted in the tracked
      S3P receipt by digest, so the evaluation chain reaches back past the gitignored
      runtime tree to sealed evidence a clean clone actually holds.
    * TRUTHFUL AUTHORITY SEMANTICS. `.1` recorded `authority.creations`, implying a
      durable token-creation event. The ledger owns no such event. Inventing one would
      have been worse; the field is gone and `plan_consumption_count` says exactly what
      the ledger can witness.
    * PORTABLE VERDICT EVIDENCE. `.1` copied `eligibility` from the report, so a clean
      clone could only repeat the claim. `.2` carries the body-free INPUTS the canonical
      decision was made from, and the verifier feeds them back into the production
      `decide_eligibility` -- one algorithm, two callers, no second implementation to
      drift.

    WHAT IT STILL NEVER CARRIES
    ---------------------------
    No prompt, no held-out target, no rubric prose, no model response, no `EVAL:`
    confirmation literal, no absolute path, no home directory and -- new in `.2` -- no
    individual held-out TASK ID. `.1` bound `holdout_commit.first_task_id`; on a live
    eval-v4 run that would have named one of the 36 frozen task ids inside a tracked
    file, which the holdout firewall refuses and which the commit's `first_task_hash`
    already establishes without naming anything.

    WHAT `receipt_hash` PROVES
    --------------------------
    Payload integrity relative to the canonical bytes, and nothing else. Not human
    identity, not human authorisation, not a commit signature, not who ran the command.
    Authenticity comes from the receipt being TRACKED and from the control plane's own
    hash chain; SHA-256 is not being oversold here.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "m62-eval-receipt-v2.schema.json",
        "title": "M62 portable evaluation receipt v2",
        "description": (
            "Root-independent, tracked evidence that ONE identified candidate, built from "
            "ONE identified training receipt and ONE identified adapter, completed ONE "
            "held-out evaluation against ONE identified baseline under ONE spent "
            "single-use authority, and that its EVALUATED_* status claim is REDERIVABLE "
            "from the body-free decision evidence it carries rather than asserted by it. "
            "It is EVIDENCE OF AN OPERATION and never AUTHORITY FOR ANOTHER: it grants no "
            "retry, no second evaluation, no promotion, no activation, no registry "
            "mutation and no release. receipt_hash proves payload integrity only -- not "
            "human authorisation, not authenticity, not who ran the command."),
        **_obj({
            "schema_version": {"const": EVAL_RECEIPT_V2_SCHEMA_VERSION},
            "receipt_version": {"const": EVAL_RECEIPT_V2_SCHEMA_VERSION},
            "receipt_hash": _SHA256,
            "evaluation_milestone": {"type": "string", "pattern": "^S[0-9A-Z.]{1,10}$"},
            "evaluation_id": _SHORT,
            "evaluation_generation": {"type": "integer", "minimum": 1,
                                      "maximum": 100_000},

            # ── where the code that measured came from ───────────────────────
            "source": _obj({
                "evaluation_source_commit": _COMMIT,
                "evaluation_source_tree_oid": _COMMIT,
                "derived_from_repository_head": {"const": True},
                "worktree_clean_at_build": {"type": "boolean"},
                "evidence_level": {"const": "the receipt was built in a worktree at this "
                                            "commit; it does not by itself prove which "
                                            "bytes executed"},
            }),

            # ── who was measured ─────────────────────────────────────────────
            "candidate": _obj({
                "candidate_id": _SHORT,
                "status_claim": {"enum": sorted(EVALUATED_CANDIDATE_STATES)},
                "identity_source": {"const": "training_receipt"},
                "adapter_reference_hash": _SHA256,
                "adapter_sha256": _SHA256,
                "adapter_manifest_hash": _SHA256,
                "adapter_artifact_set_hash": _SHA256,
            }),
            "training_receipt": _obj({
                "path": _REPO_PATH,
                "training_receipt_sha256": _SHA256,
                "schema_version": _SHORT,
                "candidate_id": _SHORT,
                "training_plan_hash": _SHA256,
                "training_source_commit": _COMMIT,
                "training_milestone": {"type": "string", "pattern": "^S[0-9A-Z.]{1,10}$"},
            }),
            "baseline": _obj({
                "model_id": _SHORT,
                "revision": _COMMIT,
                "reference_hash": _SHA256,
                "tokenizer_identity_hash": _SHA256,
                "base_model_identity_hash": _SHA256,
            }),

            # ── what it was measured on ──────────────────────────────────────
            "holdout": _obj({
                "dataset_id": _SHORT,
                "dataset_version": {"type": "string", "pattern": "^v[0-9]+$"},
                "dataset_manifest_hash": _SHA256,
                "task_pack_hash": _SHA256,
                "hidden_target_store_hash": _SHA256,
                "pack_manifest_shard_hashes": {"type": "object"},
                "split_manifest_hashes": {"type": "object"},
                "task_count": {"type": "integer", "minimum": 1, "maximum": 100_000},
                "counts_by_split": {"type": "object"},
                "counts_by_family": {"type": "object"},
                "counts_by_kind": {"type": "object"},
            }),
            "plan": _obj({
                "plan_hash": _SHA256,
                "plan_schema_version": _SHORT,
                "evaluator_version": _SHORT,
                "evaluation_config_hash": _SHA256,
                "order_policy": _SHORT,
                "order_assignment_hash": _SHA256,
                "performs_inference": {"type": "boolean"},
                "binds_exact_pack_identity": {"const": True},
                "expected_task_count": {"type": "integer", "minimum": 1,
                                        "maximum": 100_000},
            }),
            "policies": _obj({
                "generation_policy_hash": _SHA256,
                "grader_policy_hash": _SHA256,
                "metric_policy_hash": _SHA256,
                "statistical_policy_hash": _SHA256,
                "gate_policy_hash": _SHA256,
                "family_policy_hash": _SHA256,
                "dependency_report_hash": _SHA256,
                "hardware_report_hash": _SHA256,
                # D33. The CONFIGURED value and whether anything enforced it are two
                # different facts, and recording only the first is how "timeout_s: 300"
                # reads as a watchdog that does not exist.
                "configured_timeout_s": {"type": "integer", "minimum": 0,
                                         "maximum": 100_000},
                "timeout_enforced": {"type": "boolean"},
                # The whole canonical generation policy, so `generation_policy_hash` is
                # RE-DERIVABLE from the receipt rather than merely quoted by it.
                "generation_policy": _obj({
                    "policy_version": _SHORT,
                    "mode": _SHORT,
                    "max_new_tokens": {"type": "integer", "minimum": 1,
                                       "maximum": 1_000_000},
                    "max_input_tokens": {"type": "integer", "minimum": 1,
                                         "maximum": 10_000_000},
                    "do_sample": {"type": "boolean"},
                    "temperature": {"type": "number"},
                    "top_p": {"type": "number"},
                    "top_k": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
                    "repetition_penalty": {"type": "number"},
                    "stop_sequences": {"type": "array", "maxItems": 32, "items": _SHORT},
                    "seed": {"type": "integer", "minimum": 0, "maximum": 2**32 - 1},
                    "timeout_s": {"type": "integer", "minimum": 0, "maximum": 100_000},
                    "batch_size": {"type": "integer", "minimum": 1, "maximum": 4_096},
                    "truncation_side": _SHORT,
                    "reasoning_policy": _SHORT,
                    "device_policy": _SHORT,
                    "precision_policy": _SHORT,
                }),
            }),

            # ── the single-use authority, described as the ledger can witness it ──
            "authority": _obj({
                "form": {"const": "EVAL:<plan-hash>"},
                "bound_plan_hash": _SHA256,
                # NOT `creations`. There is no durable token-creation event, and a
                # receipt may not count one it cannot point at.
                "plan_consumption_count": {"type": "integer", "minimum": 1, "maximum": 1},
                "holdout_commit_count": {"type": "integer", "minimum": 1, "maximum": 1},
                "token_literal_recorded": {"const": False},
                "retry_authorized": {"const": False},
                "grants_no_further_authority": {"const": True},
                # A receipt builder could write `human_authorized: true` itself, which is
                # exactly why it never does. Human intent lives in the milestone
                # authority; this document proves execution.
                "human_authorization": {"const": "external_milestone_authority"},
            }),
            "ledger": _obj({
                "plan_started_count": {"type": "integer", "minimum": 1, "maximum": 1},
                "holdout_commit_count": {"type": "integer", "minimum": 1, "maximum": 1},
                "terminal_count": {"type": "integer", "minimum": 1, "maximum": 1},
                "terminal_event": {"enum": list(TERMINAL_EVALUATION_EVENTS)},
                "terminal_state": {"enum": list(TERMINAL_EVALUATION_EVENTS)},
                "terminal_is_successful": {"type": "boolean"},
                "events": {"type": "object"},
                # Future body-free ledger lines may coexist. They are NAMED here so they
                # are visible, and they can never become the terminal witness.
                "unrecognised_events": {"type": "array", "maxItems": 16, "items": _SHORT},
                "plan_hash": _SHA256,
                "unique_plan_hashes": {"type": "integer", "minimum": 1, "maximum": 1},
                "plan_started_event_hash": _SHA256,
                "holdout_commit_event_hash": _SHA256,
                "terminal_event_hash": _SHA256,
            }),
            "holdout_commit": _obj({
                "commit_schema_version": _SHORT,
                "pack_identity_hash": _SHA256,
                "order_assignment_hash": _SHA256,
                # `first_task_id` is DELIBERATELY ABSENT: see the class docstring.
                "first_task_hash": _SHA256,
                "first_arm": {"enum": ["baseline", "candidate"]},
                "first_request_parity_hash": _SHA256,
                "task_count": {"type": "integer", "minimum": 1, "maximum": 100_000},
                "target_count": {"type": "integer", "minimum": 0, "maximum": 100_000},
                "backend_id": _SHORT,
                "performs_inference": {"type": "boolean"},
            }),

            # ── what happened ────────────────────────────────────────────────
            "execution": _obj({
                # NOT `run_state`. A report is serialised in `comparing` or
                # `artifact_validation`; conflating that with how the RUN ended is the
                # confusion that put a spurious blocker on every live run in S3E.2.
                "report_serialization_state": _SHORT,
                "empirical_status": _SHORT,
                "backend_ids": {"type": "array", "minItems": 1, "maxItems": 8,
                                "items": _SHORT},
                "backend_version": {"type": "string", "maxLength": 200},
                "artifact_verification": {"const": "PASS"},
                "artifact_problems": {"type": "array", "maxItems": 0},
            }),
            "results": _obj({
                "expected_task_count": {"type": "integer", "minimum": 1,
                                        "maximum": 100_000},
                "task_count": {"type": "integer", "minimum": 1, "maximum": 100_000},
                "baseline_result_count": _COUNT,
                "candidate_result_count": _COUNT,
                "paired_result_count": _COUNT,
                "baseline_score_count": _COUNT,
                "candidate_score_count": _COUNT,
                "total_model_result_count": _COUNT,
                "measured_pairs": _COUNT,
                "missing_pairs": _COUNT,
                "wins": _COUNT, "ties": _COUNT, "losses": _COUNT,
            }),
            "evidence": _obj({
                "report_hash": _SHA256,
                "evaluation_manifest_hash": _SHA256,
                # NOT `artifact_tree_hash`: the ADAPTER has an artefact set digest too,
                # and two things called a tree hash in one document is one rename away
                # from being cross-checked against each other.
                "evaluation_artifact_tree_hash": _SHA256,
                "comparison_manifest_hash": _SHA256,
                "metrics_summary_hash": _SHA256,
                "pack_manifest_hash": _SHA256,
                "gate_report_hash": _SHA256,
                "bootstrap_report_hash": _SHA256,
                "files": {"type": "object"},
            }),

            # ── why the status claim follows ─────────────────────────────────
            "decision_evidence": _obj({
                "empirical_status": _SHORT,
                "report_serialization_state": _SHORT,
                "gate_report": _gate_evidence_schema(),
                "bootstrap": _bootstrap_evidence_schema(),
                "canonical_decision": _obj({
                    "eligibility": _SHORT,
                    "empirical_status": _SHORT,
                    "human_review_required": {"type": "boolean"},
                    "blockers": {"type": "array", "maxItems": 128,
                                 "items": _DECISION_TEXT},
                    "warnings": {"type": "array", "maxItems": 128,
                                 "items": _DECISION_TEXT},
                    "rationale": _DECISION_TEXT,
                    "promotes_model": {"const": False},
                    "activates_model": {"const": False},
                }),
                "decision_hash": _SHA256,
                "rederived_by": {
                    "const": "training_gym.evaluation.reports.decision_from_evidence"},
            }),
            "outcome": _obj({
                "eligibility": _SHORT,
                "human_review_required": {"type": "boolean"},
                "promotes_model": {"const": False},
                "activates_model": {"const": False},
                "mutates_model_registry": {"const": False},
                "gate_blockers": {"type": "array", "maxItems": 128,
                                  "items": _DECISION_TEXT},
                "gate_warnings": {"type": "array", "maxItems": 128,
                                  "items": _DECISION_TEXT},
                "limitations": {"type": "array", "maxItems": 64, "items": _DECISION_TEXT},
            }),
        }),
    }


def eval_receipt_v3_schema() -> dict:
    """S3Q.0.2 -- the POST-LIVE SEAL RECOVERY contract.

    `.3` exists because `.2` met real evidence for the first time and refused it three
    times. Every one of those refusals was `.2` being wrong about production, never
    production being wrong. Nothing in the measurement was edited to satisfy a receipt:
    the repair moved the CONTRACT to the evidence.

    WHAT `.3` CHANGES, AND WHY EACH IS A CONTRACT CHANGE RATHER THAN A PATCH
    ------------------------------------------------------------------------
    * THE VERDICT PARTITION IS THE PRODUCTION ONE. `.2` required
      `wins + ties + losses == measured_pairs`. `comparison.py` classifies FOUR
      comparable verdicts, and the fourth -- `security_improvement`, the baseline having
      had a blocking finding the candidate fixed -- is deliberately not a win. On the
      real S3Q run the three-way sum is 33 against 36 measured pairs, so `.2` called a
      correct measurement inconsistent. `.3` carries `verdict_counts` over the exact
      production vocabulary, requires it to sum to `measured_pairs`, and keeps
      `wins/ties/losses` only as the aliases they are -- cross-checked against
      `improved/unchanged/regressed`, never summed against the total.
    * THE NUMERIC PARTITION IS SEPARATE AND SAYS SO. The sign of the reward delta is a
      DIFFERENT partition of the same pairs (13/13/10 where the verdicts are 11/12/10/3).
      `.3` carries it under its own name and verifies it on its own. The two are never
      compared bucket for bucket; only their totals must agree.
    * THE CANONICAL BYTES ARE DEFINED, NOT CONSTRAINED. `.2` refused any receipt whose
      canonical text was not ASCII, on the reasoning that otherwise the bytes depend on
      an encoding choice. The reasoning was right and the remedy was backwards: it
      removed the ambiguity by removing legitimate evidence. A production gate message
      reads `schema validity fell from 1.0000 to 0.8889 (U+2212 0.1111)` and that minus
      sign is the report's, correctly typeset by `gates.py`. `.3` closes the ambiguity
      the other way -- the canonical bytes ARE canonical JSON encoded UTF-8, stated in
      the receipt itself, and `receipt_hash` is SHA-256 over exactly those bytes. No
      choice remains, and no evidence is normalised away to reach that. The token,
      private-path, body-symbol and task-id scanners are untouched: Unicode is permitted,
      not privileged.
    * TWO SOURCES, BECAUSE THERE HONESTLY ARE TWO. `.2` had one `source` block whose
      `evaluation_source_commit` was the repository HEAD when the RECEIPT was built. That
      is the evaluation source only while sealing happens at the unchanged evaluated
      commit -- true until sealing failed. `.3` separates `evaluation_source`, bound
      through the pre-repair measurement witness, from `seal_implementation_source`, the
      commit that carries this code. In a recovery they DIFFER, and a contract that
      cannot express that is a contract that forces a receipt to lie about one of them.

    WHAT `.3` DOES NOT WEAKEN
    -------------------------
    Every `.2` guarantee is carried forward unchanged: mandatory non-empty adapter
    identity, the training receipt as the candidate's identity root, exact holdout and
    pack identity, the policy digests with the generation policy re-derivable from the
    values beside it, one plan consumption, one model-facing commit, one recognised
    terminal event each bound by its own digest, a single plan hash, the result counts,
    the artefact digests, and an eligibility REDERIVED by the production decision
    algorithm rather than copied from the report.

    WHAT IT STILL NEVER CARRIES
    ---------------------------
    No prompt, no held-out target, no rubric prose, no model response, no individual task
    id, no `EVAL:` confirmation literal, no absolute path and no home directory.

    WHAT THE SOURCE BINDING PROVES, AND WHAT IT DOES NOT
    ----------------------------------------------------
    The witness, its Git first parent and the immutable report digests establish
    REPOSITORY PROVENANCE: which tracked source state the measurement belongs to, fixed
    before the repair could move HEAD. They do NOT prove CPU-level execution
    authenticity, and nothing here is signed. No PKI is invented and none is implied.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "m62-eval-receipt-v3.schema.json",
        "title": "M62 portable evaluation receipt v3",
        "description": (
            "Root-independent, tracked evidence that ONE identified candidate, built from "
            "ONE identified training receipt and ONE identified adapter, completed ONE "
            "held-out evaluation against ONE identified baseline under ONE spent "
            "single-use authority, and that its EVALUATED_* status claim is REDERIVABLE "
            "from the body-free decision evidence it carries rather than asserted by it. "
            "v3 seals an EXISTING measurement after the fact: it binds the evaluation "
            "source through a pre-repair measurement witness and names the seal "
            "implementation source separately, because after a post-live repair they are "
            "legitimately different commits. Its canonical bytes are canonical JSON "
            "encoded UTF-8, so production decision text is preserved exactly. It is "
            "EVIDENCE OF AN OPERATION and never AUTHORITY FOR ANOTHER: it grants no "
            "retry, no second evaluation, no promotion, no activation, no registry "
            "mutation and no release. receipt_hash proves payload integrity only -- not "
            "human authorisation, not execution authenticity, not who ran the command."),
        **_obj({
            "schema_version": {"const": EVAL_RECEIPT_V3_SCHEMA_VERSION},
            "receipt_version": {"const": EVAL_RECEIPT_V3_SCHEMA_VERSION},
            "receipt_hash": _SHA256,
            "canonical_encoding": {"const": CANONICAL_RECEIPT_ENCODING},
            "evaluation_milestone": {"type": "string", "pattern": "^S[0-9A-Z.]{1,10}$"},
            "seal_milestone": {"type": "string", "pattern": "^S[0-9A-Z.]{1,10}$"},
            "evaluation_id": _SHORT,
            "evaluation_generation": {"type": "integer", "minimum": 1,
                                      "maximum": 100_000},

            # ── the code that MEASURED, bound through the pre-repair witness ──
            "evaluation_source": _obj({
                "evaluation_source_commit": _COMMIT,
                "evaluation_source_tree_oid": _COMMIT,
                "evaluation_source_digest": _SHA256,
                # NOT `derived_from_repository_head`. That is precisely the conflation
                # `.3` exists to remove.
                "derived_from": {"const": "measurement_witness"},
                "evidence_level": {
                    "const": "the measurement witness was written in a clean worktree at "
                             "this commit, before the repair moved HEAD, and its Git "
                             "first parent is this commit; that is repository "
                             "provenance and not proof of which bytes executed"},
            }),

            # ── the code that SEALED. A different commit, and honestly named ──
            "seal_implementation_source": _obj({
                "seal_implementation_source_commit": _COMMIT,
                "seal_implementation_tree_oid": _COMMIT,
                "derived_from_repository_head": {"const": True},
                "worktree_clean_at_build": {"const": True},
                "differs_from_evaluation_source": {"type": "boolean"},
                "evidence_level": {
                    "const": "this is the tracked code that BUILT the receipt; it did not "
                             "measure, and recording it as the evaluation source is the "
                             "defect m62.eval_receipt.3 exists to fix"},
            }),

            # ── the bridge between them ──────────────────────────────────────
            "measurement_witness": _obj({
                "path": _REPO_PATH,
                "measurement_witness_sha256": _SHA256,
                "measurement_witness_hash": _SHA256,
                "measurement_witness_commit": _COMMIT,
                "witness_schema_version": {"const": MEASUREMENT_WITNESS_SCHEMA_VERSION},
                "witness_first_parent_is_evaluation_source": {"const": True},
                "grants_no_authority": {"const": True},
            }),

            # ── who was measured ─────────────────────────────────────────────
            "candidate": _obj({
                "candidate_id": _SHORT,
                "status_claim": {"enum": sorted(EVALUATED_CANDIDATE_STATES)},
                "identity_source": {"const": "training_receipt"},
                "adapter_reference_hash": _SHA256,
                "adapter_sha256": _SHA256,
                "adapter_manifest_hash": _SHA256,
                "adapter_artifact_set_hash": _SHA256,
            }),
            "training_receipt": _obj({
                "path": _REPO_PATH,
                "training_receipt_sha256": _SHA256,
                "schema_version": _SHORT,
                "candidate_id": _SHORT,
                "training_plan_hash": _SHA256,
                "training_source_commit": _COMMIT,
                "training_milestone": {"type": "string", "pattern": "^S[0-9A-Z.]{1,10}$"},
            }),
            "baseline": _obj({
                "model_id": _SHORT,
                "revision": _COMMIT,
                "reference_hash": _SHA256,
                "tokenizer_identity_hash": _SHA256,
                "base_model_identity_hash": _SHA256,
            }),

            # ── what it was measured on ──────────────────────────────────────
            "holdout": _obj({
                "dataset_id": _SHORT,
                "dataset_version": {"type": "string", "pattern": "^v[0-9]+$"},
                "dataset_manifest_hash": _SHA256,
                "task_pack_hash": _SHA256,
                "hidden_target_store_hash": _SHA256,
                "pack_manifest_shard_hashes": {"type": "object"},
                "split_manifest_hashes": {"type": "object"},
                "task_count": {"type": "integer", "minimum": 1, "maximum": 100_000},
                "counts_by_split": {"type": "object"},
                "counts_by_family": {"type": "object"},
                "counts_by_kind": {"type": "object"},
            }),
            "plan": _obj({
                "plan_hash": _SHA256,
                "plan_schema_version": _SHORT,
                "evaluator_version": _SHORT,
                "evaluation_config_hash": _SHA256,
                "order_policy": _SHORT,
                "order_assignment_hash": _SHA256,
                "performs_inference": {"type": "boolean"},
                "binds_exact_pack_identity": {"const": True},
                "expected_task_count": {"type": "integer", "minimum": 1,
                                        "maximum": 100_000},
            }),
            "policies": _obj({
                "generation_policy_hash": _SHA256,
                "grader_policy_hash": _SHA256,
                "metric_policy_hash": _SHA256,
                "statistical_policy_hash": _SHA256,
                "gate_policy_hash": _SHA256,
                "family_policy_hash": _SHA256,
                "dependency_report_hash": _SHA256,
                "hardware_report_hash": _SHA256,
                # D33, unchanged. The CONFIGURED value and whether anything enforced it
                # are two different facts.
                "configured_timeout_s": {"type": "integer", "minimum": 0,
                                         "maximum": 100_000},
                "timeout_enforced": {"const": False},
                "generation_policy": _obj({
                    "policy_version": _SHORT,
                    "mode": _SHORT,
                    "max_new_tokens": {"type": "integer", "minimum": 1,
                                       "maximum": 1_000_000},
                    "max_input_tokens": {"type": "integer", "minimum": 1,
                                         "maximum": 10_000_000},
                    "do_sample": {"type": "boolean"},
                    "temperature": {"type": "number"},
                    "top_p": {"type": "number"},
                    "top_k": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
                    "repetition_penalty": {"type": "number"},
                    "stop_sequences": {"type": "array", "maxItems": 32, "items": _SHORT},
                    "seed": {"type": "integer", "minimum": 0, "maximum": 2**32 - 1},
                    "timeout_s": {"type": "integer", "minimum": 0, "maximum": 100_000},
                    "batch_size": {"type": "integer", "minimum": 1, "maximum": 4_096},
                    "truncation_side": _SHORT,
                    "reasoning_policy": _SHORT,
                    "device_policy": _SHORT,
                    "precision_policy": _SHORT,
                }),
            }),

            # ── the single-use authority, described as the ledger can witness it ──
            "authority": _obj({
                "form": {"const": "EVAL:<plan-hash>"},
                "bound_plan_hash": _SHA256,
                "plan_consumption_count": {"type": "integer", "minimum": 1, "maximum": 1},
                "holdout_commit_count": {"type": "integer", "minimum": 1, "maximum": 1},
                "token_literal_recorded": {"const": False},
                "retry_authorized": {"const": False},
                "grants_no_further_authority": {"const": True},
                "human_authorization": {"const": "external_milestone_authority"},
                # S3Q.0.2. Sealing an EXISTING measurement consumes nothing: the
                # authority was spent by the run this receipt describes, long before the
                # receipt existed. Saying so is what stops a re-seal reading as a re-run.
                "spent_by_the_run_this_receipt_describes": {"const": True},
                "seal_consumed_no_authority": {"const": True},
            }),
            "ledger": _obj({
                "plan_started_count": {"type": "integer", "minimum": 1, "maximum": 1},
                "holdout_commit_count": {"type": "integer", "minimum": 1, "maximum": 1},
                "terminal_count": {"type": "integer", "minimum": 1, "maximum": 1},
                "terminal_event": {"enum": list(TERMINAL_EVALUATION_EVENTS)},
                "terminal_state": {"enum": list(TERMINAL_EVALUATION_EVENTS)},
                "terminal_is_successful": {"type": "boolean"},
                "events": {"type": "object"},
                "unrecognised_events": {"type": "array", "maxItems": 16, "items": _SHORT},
                "plan_hash": _SHA256,
                "unique_plan_hashes": {"type": "integer", "minimum": 1, "maximum": 1},
                "plan_started_event_hash": _SHA256,
                "holdout_commit_event_hash": _SHA256,
                "terminal_event_hash": _SHA256,
            }),
            "holdout_commit": _obj({
                "commit_schema_version": _SHORT,
                "pack_identity_hash": _SHA256,
                "order_assignment_hash": _SHA256,
                # `first_task_id` remains DELIBERATELY ABSENT.
                "first_task_hash": _SHA256,
                "first_arm": {"enum": ["baseline", "candidate"]},
                "first_request_parity_hash": _SHA256,
                "task_count": {"type": "integer", "minimum": 1, "maximum": 100_000},
                "target_count": {"type": "integer", "minimum": 0, "maximum": 100_000},
                "backend_id": _SHORT,
                "performs_inference": {"type": "boolean"},
            }),

            # ── what happened ────────────────────────────────────────────────
            "execution": _obj({
                "report_serialization_state": _SHORT,
                "empirical_status": _SHORT,
                "backend_ids": {"type": "array", "minItems": 1, "maxItems": 8,
                                "items": _SHORT},
                "backend_version": {"type": "string", "maxLength": 200},
                "artifact_verification": {"const": "PASS"},
                "artifact_problems": {"type": "array", "maxItems": 0},
                # S3Q.0.2. Nothing here re-ran, re-scored or re-generated anything.
                "sealed_from_existing_measurement": {"const": True},
                "model_loads_during_seal": {"const": 0},
                "model_generations_during_seal": {"const": 0},
            }),
            "results": _obj({
                "expected_task_count": {"type": "integer", "minimum": 1,
                                        "maximum": 100_000},
                "task_count": {"type": "integer", "minimum": 1, "maximum": 100_000},
                "baseline_result_count": _COUNT,
                "candidate_result_count": _COUNT,
                "paired_result_count": _COUNT,
                "baseline_score_count": _COUNT,
                "candidate_score_count": _COUNT,
                "total_model_result_count": _COUNT,
                "measured_pairs": _COUNT,
                "missing_pairs": _COUNT,
                # The production partition. Exhaustive, and the one that must sum.
                "verdict_counts": _verdict_counts_schema(),
                "verdict_vocabulary": {"type": "array", "minItems": 1, "maxItems": 16,
                                       "items": _SHORT, "uniqueItems": True},
                # A DIFFERENT partition of the same pairs. Verified separately.
                "numeric_delta_counts": _numeric_delta_counts_schema(),
                # Aliases for three of the four verdicts, kept because the report
                # publishes them and a disagreement between the two surfaces is worth
                # catching. NEVER summed against `measured_pairs`.
                "wins": _COUNT, "ties": _COUNT, "losses": _COUNT,
                "wins_ties_losses_are_a_partial_partition": {"const": True},
            }),
            "evidence": _obj({
                "report_hash": _SHA256,
                "evaluation_manifest_hash": _SHA256,
                "evaluation_artifact_tree_hash": _SHA256,
                "comparison_manifest_hash": _SHA256,
                "metrics_summary_hash": _SHA256,
                "pack_manifest_hash": _SHA256,
                "gate_report_hash": _SHA256,
                "bootstrap_report_hash": _SHA256,
                "files": {"type": "object"},
            }),

            # ── why the status claim follows ─────────────────────────────────
            "decision_evidence": _obj({
                "empirical_status": _SHORT,
                "report_serialization_state": _SHORT,
                "gate_report": _gate_evidence_schema(),
                "bootstrap": _bootstrap_evidence_schema(),
                "canonical_decision": _obj({
                    "eligibility": _SHORT,
                    "empirical_status": _SHORT,
                    "human_review_required": {"type": "boolean"},
                    "blockers": {"type": "array", "maxItems": 128,
                                 "items": _DECISION_TEXT},
                    "warnings": {"type": "array", "maxItems": 128,
                                 "items": _DECISION_TEXT},
                    "rationale": _DECISION_TEXT,
                    "promotes_model": {"const": False},
                    "activates_model": {"const": False},
                }),
                "decision_hash": _SHA256,
                "rederived_by": {
                    "const": "training_gym.evaluation.reports.decision_from_evidence"},
                # S3Q.0.2. The receipt states whether the production decision text it
                # carries is pure ASCII, so a reader knows the UTF-8 definition is doing
                # work here rather than being a dormant clause.
                "carries_non_ascii_decision_text": {"type": "boolean"},
                "non_ascii_codepoints": {"type": "array", "maxItems": 32,
                                         "items": {"type": "string", "minLength": 1,
                                                   "maxLength": 16},
                                         "uniqueItems": True},
            }),
            "outcome": _obj({
                "eligibility": _SHORT,
                "human_review_required": {"type": "boolean"},
                "promotes_model": {"const": False},
                "activates_model": {"const": False},
                "mutates_model_registry": {"const": False},
                "gate_blockers": {"type": "array", "maxItems": 128,
                                  "items": _DECISION_TEXT},
                "gate_warnings": {"type": "array", "maxItems": 128,
                                  "items": _DECISION_TEXT},
                "limitations": {"type": "array", "maxItems": 64, "items": _DECISION_TEXT},
                "security_blocking_count": _COUNT,
            }),
        }),
    }


def measurement_witness_schema() -> dict:
    """S3Q.0.2 -- the pre-repair measurement witness.

    WHY A SECOND EVIDENCE FORM EXISTS AT ALL
    ----------------------------------------
    A portable receipt is written AFTER the irreversible act, and until S3Q.0.2 the code
    that wrote it also had to BE the code that measured -- because the only thing naming
    the evaluation source was the repository HEAD at receipt-build time. That holds
    exactly as long as sealing succeeds on the first attempt. It did not. Repairing the
    receipt requires a commit, a commit moves HEAD, and after it there is nothing left in
    the repository that can say which source state measured.

    So this document is written FIRST, alone, while HEAD is still the evaluated commit,
    and its Git first parent IS that commit. It is the bridge across the repair.

    WHAT IT IS NOT
    --------------
    Not a receipt. It grants no candidate state, authorises no retry, promotes nothing and
    claims no eligibility ON BEHALF of anything -- it RECORDS the eligibility the
    production algorithm rederives from the report's own body-free evidence. A reader who
    holds only this document knows what was measured and is permitted to do nothing.

    WHAT IT NEVER CARRIES
    ---------------------
    No prompt, no held-out target, no model response, no individual task id, no
    confirmation literal, no absolute path.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "m62-measurement-witness.schema.json",
        "title": "M62 pre-repair measurement witness",
        "description": (
            "Body-free evidence, written while the repository was still at the evaluation "
            "source commit, recording which existing runtime measurement belongs to that "
            "unchanged source state. It is a BRIDGE across a post-live repair commit and "
            "never an authority: it grants no candidate state, no retry, no second "
            "evaluation, no promotion, no activation and no registry mutation."),
        **_obj({
            "schema_version": {"const": MEASUREMENT_WITNESS_SCHEMA_VERSION},
            "witness_version": {"const": MEASUREMENT_WITNESS_SCHEMA_VERSION},
            "witness_kind": {"const": "pre_repair_measurement_witness"},
            "witness_hash": _SHA256,
            "milestone": {"type": "string", "pattern": "^S[0-9A-Z.]{1,10}$"},
            "purpose": _DECISION_TEXT,
            "grants": _obj({
                "candidate_state": {"const": False},
                "promotion": {"const": False},
                "activation": {"const": False},
                "registry_mutation": {"const": False},
                "retry_or_rerun": {"const": False},
                "is_an_evaluation_receipt": {"const": False},
                "note": _SHORT,
            }),
            "evaluation_id": _SHORT,
            "evaluation_generation": {"type": "integer", "minimum": 1,
                                      "maximum": 100_000},
            "candidate_id": _SHORT,
            "evaluation_source": _obj({
                "evaluation_source_commit": _COMMIT,
                "evaluation_source_tree_oid": _COMMIT,
                "evaluation_source_digest": _SHA256,
                "evaluation_source_digest_method": _DECISION_TEXT,
                "evaluation_source_file_count": {"type": "integer", "minimum": 1,
                                                 "maximum": 100_000},
                "worktree_clean_at_witness": {"const": True},
                "derived_from_repository_head": {"const": True},
                "evidence_level": _DECISION_TEXT,
            }),
            "eval_corpus": _obj({
                "dataset_id": _SHORT,
                "dataset_version": {"type": "string", "pattern": "^v[0-9]+$"},
                "dataset_manifest_hash": _SHA256,
                "task_pack_hash": _SHA256,
                "hidden_target_store_hash": _SHA256,
                "pack_manifest_hash": _SHA256,
                "status_claim": {"const": "USED_IMMUTABLE"},
                "spent_once": {"const": True},
            }),
            "plan": _obj({
                "plan_hash": _SHA256,
                "plan_schema_version": _SHORT,
                "evaluation_config_hash": _SHA256,
                "order_assignment_hash": _SHA256,
                "expected_task_count": {"type": "integer", "minimum": 1,
                                        "maximum": 100_000},
                "performs_inference": {"type": "boolean"},
            }),
            "evidence": _obj({
                "report_hash": _SHA256,
                "evaluation_manifest_hash": _SHA256,
                "evaluation_artifact_tree_hash": _SHA256,
                "comparison_manifest_hash": _SHA256,
                "metrics_summary_hash": _SHA256,
                "gate_report_hash": _SHA256,
                "bootstrap_report_hash": _SHA256,
                "files": {"type": "object"},
            }),
            "ledger": _obj({
                "plan_started_count": {"type": "integer", "minimum": 1, "maximum": 1},
                "holdout_commit_count": {"type": "integer", "minimum": 1, "maximum": 1},
                "terminal_count": {"type": "integer", "minimum": 1, "maximum": 1},
                "terminal_event": {"enum": list(TERMINAL_EVALUATION_EVENTS)},
                "unique_plan_hashes": {"type": "integer", "minimum": 1, "maximum": 1},
                "plan_hash": _SHA256,
                "unrecognised_events": {"type": "array", "maxItems": 16, "items": _SHORT},
                "plan_started_event_hash": _SHA256,
                "holdout_commit_event_hash": _SHA256,
                "terminal_event_hash": _SHA256,
            }),
            "results": _obj({
                "task_count": {"type": "integer", "minimum": 1, "maximum": 100_000},
                "measured_pairs": _COUNT,
                "missing_pairs": _COUNT,
                "baseline_result_count": _COUNT,
                "candidate_result_count": _COUNT,
                "paired_result_count": _COUNT,
                "baseline_score_count": _COUNT,
                "candidate_score_count": _COUNT,
                "total_model_result_count": _COUNT,
                "verdict_counts": _verdict_counts_schema(required=False),
                "numeric_delta_counts": _numeric_delta_counts_schema(),
            }),
            "outcome": _obj({
                "canonical_eligibility": _SHORT,
                "decision_hash": _SHA256,
                "human_review_required": {"type": "boolean"},
                "rederived_by": {
                    "const": "training_gym.evaluation.reports.decision_from_evidence"},
                "promotes_model": {"const": False},
                "activates_model": {"const": False},
                "mutates_model_registry": {"const": False},
            }),
            "receipt_v2_seal_failure_classes": {
                "type": "array", "minItems": 1, "maxItems": 16, "items": _SHORT},
        }),
    }


def _verdict_counts_schema(*, required: bool = True) -> dict:
    """The canonical paired verdict partition, one key per PRODUCTION verdict.

    Exhaustive by construction when *required*: a receipt states `security_regression: 0`
    rather than omitting it, because "there were none" is a positive claim about the one
    verdict that is a veto, and an absent key is not one. The witness predates that rule
    and carries only the verdicts it observed, so it asks for the looser form.
    """
    return _obj({verdict: _COUNT for verdict in COMPARISON_VERDICTS},
                required=list(COMPARISON_VERDICTS) if required else [])


def _numeric_delta_counts_schema() -> dict:
    """The sign of the reward delta, per measured pair.

    A DIFFERENT partition from the verdicts and never asserted equal to it bucket for
    bucket: a pair can improve numerically and still be classified `security_improvement`,
    and one can be `unchanged` on the verdict while carrying a delta that is not zero.
    Only the TOTALS have to agree, and only because both partitions cover the same pairs.
    """
    return _obj({"positive": _COUNT, "zero": _COUNT, "negative": _COUNT})


def snapshot_v3_schema() -> dict:
    """The CONTENT-ADDRESSED generation schema (V69 M63).

    Strict and closed like the V2 schema. It describes the document AS STORED;
    the rehydrated form is separately validated against :func:`snapshot_schema`,
    so a V3 generation has to satisfy BOTH — the new container and the old
    semantics. That is what makes the migration representation-only.
    """
    base = snapshot_schema()
    inline = {k: v for k, v in base["properties"].items()
              if k not in V3_RECORD_BLOCKS}
    inline["schema_version"] = {"const": CONTROL_PLANE_V3_SCHEMA_VERSION}
    inline["records"] = {
        "type": "object", "additionalProperties": False,
        "required": list(V3_RECORD_BLOCKS),
        "properties": {name: _SHA256 for name in V3_RECORD_BLOCKS},
    }
    required = [k for k in base.get("required", []) if k not in V3_RECORD_BLOCKS]
    return {"type": "object", "additionalProperties": False,
            "required": sorted(set(required) | {"records", "schema_version"}),
            "properties": inline}


def snapshot_schema() -> dict:
    """The state schema. Strict, closed and enum-bound in every security-relevant place."""
    dataset = _obj({
        "dataset_id": _SHORT,
        "version": {"type": "string", "pattern": "^v[0-9]+$"},
        "role": {"enum": list(DATASET_ROLES)},
        "status": {"enum": list(DATASET_STATES)},
        "manifest_hash": _SHA256,
        "parent_manifest_hash": {"oneOf": [{"type": "null"}, _SHA256]},
        "pack_hash": {"oneOf": [{"type": "null"}, _SHA256]},
        "task_count": {"type": "integer", "minimum": 1, "maximum": 100_000},
        "spent_by": {"oneOf": [{"type": "null"}, _SHORT]},
        "evidence": _REPO_PATH,
    })
    candidate = _obj({
        "candidate_id": _SHORT,
        "ordinal": {"type": "integer", "minimum": 1, "maximum": 1_000},
        "status": {"enum": list(CANDIDATE_STATES)},
        "base_model_revision": {"oneOf": [{"type": "null"}, _COMMIT]},
        "adapter_sha256": {"oneOf": [{"type": "null"}, _SHA256]},
        "adapter_manifest_hash": {"oneOf": [{"type": "null"}, _SHA256]},
        "training_corpus": {"oneOf": [{"type": "null"}, _SHORT]},
        "evaluation_corpus": {"oneOf": [{"type": "null"}, _SHORT]},
        "evidence": {"oneOf": [{"type": "null"}, _REPO_PATH]},
        # S3P. A pointer to the tracked, root-independent receipt that establishes the
        # candidate really completed its one authorised training run. `null` until a
        # candidate is trained; REQUIRED from TRAINED_UNEVALUATED onward, because a
        # trained claim the control plane cannot back is exactly the claim it must not
        # be able to make.
        "training_receipt": {"oneOf": [{"type": "null"}, _REPO_PATH]},
        # S3Q.0. A pointer to the tracked, root-independent receipt that establishes the
        # candidate really completed one held-out evaluation.
        #
        # OPTIONAL BY SHAPE, MANDATORY BY SEMANTICS. It is absent from the `required`
        # list below so that snapshots written before S3Q.0 stay structurally valid --
        # they describe a world in which no candidate had been evaluated under this
        # evidence form, and rewriting a superseded snapshot to add a field is exactly
        # the immutability violation this control plane forbids.
        #
        # Absence is NOT permission. `check_evaluation_receipt` refuses any EVALUATED_*
        # candidate outside LEGACY_EVALUATION_CANDIDATES that does not carry one, so a
        # missing pointer fails closed rather than reading as "no receipt required".
        # MISSING is not FALSE.
        "evaluation_receipt": {"oneOf": [{"type": "null"}, _REPO_PATH]},
    }, required=["candidate_id", "ordinal", "status", "base_model_revision",
                 "adapter_sha256", "adapter_manifest_hash", "training_corpus",
                 "evaluation_corpus", "evidence", "training_receipt"])
    defect = _obj({
        "id": {"type": "string", "pattern": "^D[0-9]{1,3}$"},
        "status": {"enum": list(DEFECT_STATES)},
        "is_gate": {"type": "boolean"},
        "summary": _SHORT,
        "evidence": {"oneOf": [{"type": "null"}, _REPO_PATH]},
    })
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "m62-snapshot.schema.json",
        "title": "M62 control-plane state snapshot",
        "description": (
            "An append-only, hash-chained cache of CURRENT M62 facts. It is a CLAIM, not "
            "an authority: every field with an independent source is cross-checked by "
            "scripts/verify_m62_control_plane.py. It cannot grant TRAIN, EVAL, promotion, "
            "registry mutation or release, and it holds no task material."),
        **_obj({
            "schema_version": {"const": CONTROL_PLANE_SCHEMA_VERSION},
            "state_generation": {"type": "integer", "minimum": 1, "maximum": 100_000},
            "generation_label": {"type": "string", "pattern": "^[A-Z0-9_]{3,64}$"},
            "parent_snapshot_sha256": {"oneOf": [{"type": "null"}, _SHA256]},
            "subject_state_commit": _COMMIT,
            "subject_state_milestone": {"type": "string", "pattern": "^S[0-9A-Z.]{1,10}$"},
            "control_plane_note": _SHORT,
            "archive": _obj({
                "path": _REPO_PATH, "sha256": _SHA256,
                "bytes": {"type": "integer", "minimum": 1},
                "lines": {"type": "integer", "minimum": 1},
                "immutable": {"const": True},
            }),
            "project": _obj({
                "branch": _SHORT, "master_commit": _COMMIT, "milestone": _SHORT,
                "merged_into_master": {"const": False},
                "tagged": {"const": False},
                "released": {"const": False},
            }),
            "base_model": _obj({
                "model_id": _SHORT, "revision": _COMMIT, "revision_kind": _SHORT,
                "chat_template_digest": _SHA256,
                "training_precision": _SHORT,
                "evidence": _REPO_PATH,
            }),
            "datasets": {"type": "array", "minItems": 1, "maxItems": 64, "items": dataset},
            "candidates": {"type": "array", "minItems": 1, "maxItems": 64,
                           "items": candidate},
            "policy_identities": _obj({
                "gate_policy_hash": _SHA256,
                "metric_policy_hash": _SHA256,
                "generation_policy_hash": _SHA256,
                "generation_policy_constructor_default_hash": _SHA256,
                "reasoning_policy": {"enum": ["DISABLED", "ENABLED", "MODEL_DEFAULT"]},
                "max_new_tokens": {"type": "integer", "minimum": 1, "maximum": 8192},
                "note": _SHORT,
            }),
            "defects": {"type": "array", "minItems": 1, "maxItems": 64, "items": defect},
            "limitations": {"type": "array", "minItems": 1, "maxItems": 64,
                            "items": _SHORT},
            "authority_observation": _obj({
                "train": {"enum": list(AUTHORITY_OBSERVATIONS)},
                "eval": {"enum": list(AUTHORITY_OBSERVATIONS)},
                "promotion": {"enum": list(AUTHORITY_OBSERVATIONS)},
                "control_plane_can_grant_authority": {"const": False},
                "note": _SHORT,
            }),
            "test_baseline": _obj({
                "invocation": _SHORT,
                "working_directory": _SHORT,
                "passed": {"type": "integer", "minimum": 0},
                "skipped": {"type": "integer", "minimum": 0},
                "failed": {"type": "integer", "minimum": 0},
                "milestone": _SHORT,
                "known_invocation_artifact": _obj({
                    "invocation": _SHORT,
                    "failing_tests": {"type": "integer", "minimum": 0},
                    "file": _SHORT,
                    "is_a_regression": {"const": False},
                    "is_defect_d39": {"const": False},
                    "note": _SHORT,
                }),
            }),
            "frozen_invariants": {"type": "array", "minItems": 1, "maxItems": 64,
                                  "items": _SHORT},
            "next_milestone": _obj({
                "name": _SHORT,
                "requires_new_session": {"type": "boolean"},
                "primary_axis": _SHORT,
                "lora_scope": _SHORT,
                "training_corpus": _SHORT,
                "evaluation_holdout": _SHORT,
                "holdout_access": _SHORT,
                "ruled_out": {"type": "array", "minItems": 1, "maxItems": 32,
                              "items": _SHORT},
                "authority_required": {"type": "array", "minItems": 1, "maxItems": 8,
                                       "items": _SHORT},
            }),
        }),
    }


# ── Problem reporting ────────────────────────────────────────────────────────────────
CATEGORIES = (
    "SCHEMA", "CURRENT_POINTER", "SNAPSHOT_CHAIN", "ARCHIVE_INTEGRITY", "GIT_AUTHORITY",
    "DATASET_STATE", "CANDIDATE_STATE", "TRAINING_RECEIPT", "EVALUATION_RECEIPT",
    "POLICY_IDENTITIES",
    "AUTHORITY_SEPARATION", "HOLDOUT_FIREWALL", "PATH_INTEGRITY", "STALE_STATE",
    "RECORD_STORE",
    "CONTROL_PLANE_BUDGET",
)


@dataclass
class Report:
    problems: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def fail(self, category: str, message: str) -> None:
        assert category in CATEGORIES, category  # nosec B101 - developer invariant
        self.problems.append((category, message))

    def note(self, message: str) -> None:
        self.notes.append(message)

    def status(self, category: str) -> str:
        return "FAIL" if any(c == category for c, _ in self.problems) else "PASS"

    @property
    def ok(self) -> bool:
        return not self.problems


# ── Git, read-only ───────────────────────────────────────────────────────────────────
def _git(*args: str) -> "tuple[int, str]":
    """Run one read-only git plumbing command. Fixed argv, no shell, no network."""
    try:
        done = subprocess.run(  # nosec B603 - fixed argv list, shell=False, no user input
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True, text=True, timeout=60, check=False, shell=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return done.returncode, done.stdout.strip()


# ── Loading ──────────────────────────────────────────────────────────────────────────
@dataclass
class ControlPlane:
    current: dict
    current_bytes: bytes
    #: ALWAYS the V2-shaped document. For a V3 generation this is the rehydrated
    #: form, so every check below reads one shape regardless of what is on disk.
    snapshot: dict
    #: The bytes actually on disk — what the budget is measured against and what
    #: the next generation's parent digest covers.
    snapshot_bytes: bytes
    snapshot_path: Path
    migration: dict
    #: The on-disk document as written. Identical to ``snapshot`` under V2.
    snapshot_stored: dict = field(default_factory=dict)
    #: digest -> record payload, for a V3 generation. Empty under V2.
    records: dict = field(default_factory=dict)
    #: Problems found while rehydrating. Non-empty means the generation could
    #: not be read as it claims, and the run must fail rather than proceed on a
    #: partially-reconstructed state.
    rehydration_problems: tuple = ()

    @property
    def is_v3(self) -> bool:
        return self.snapshot_stored.get(
            "schema_version") == CONTROL_PLANE_V3_SCHEMA_VERSION


def _load_json(path: Path, label: str) -> "tuple[dict, bytes]":
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"S3N1_CONTROL_PLANE_UNREADABLE: {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"S3N1_CONTROL_PLANE_UNREADABLE: {label} is not an object")
    return payload, raw


#: The blocks a V3 generation references by digest instead of storing inline.
#: Mirrors ``scripts/migrate_m62_control_plane_v3.RECORD_BLOCKS``; a test pins
#: the two lists equal so they cannot drift into two different contracts.
V3_RECORD_BLOCKS = (
    "archive", "base_model", "candidates", "datasets", "defects",
    "frozen_invariants", "limitations", "policy_identities",
)


def load_record_store(directory: Path) -> "dict[str, dict]":
    """Read the content-addressed record store.

    The filename is NOT trusted as the address: each record is re-hashed from
    its own canonical bytes, so a file renamed to impersonate another record
    simply does not resolve.
    """
    out: dict[str, dict] = {}
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_bytes().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            out[sha256_bytes(canonical_bytes(payload))] = payload
    return out


def rehydrate_v3(stored: dict, records: "dict[str, dict]") -> "tuple[dict, tuple]":
    """Reconstruct the V2-shaped document from a V3 generation.

    Returns ``(payload, problems)``. Fails closed and reports rather than
    raising, so a broken record store produces a verifier FAILURE with a reason
    instead of a traceback.
    """
    problems: list[str] = []
    reference = stored.get("records")
    if not isinstance(reference, dict):
        return {}, ("a V3 generation carries no records map",)
    if set(reference) != set(V3_RECORD_BLOCKS):
        problems.append(
            f"the records map names {sorted(reference)}, the contract names "
            f"{sorted(V3_RECORD_BLOCKS)}")

    out = {k: v for k, v in stored.items() if k != "records"}
    out["schema_version"] = CONTROL_PLANE_SCHEMA_VERSION
    for name in sorted(reference):
        digest = reference[name]
        if not isinstance(digest, str) or not SHA256_RE.match(digest):
            problems.append(f"block {name!r} is referenced by {digest!r}, not a sha256")
            continue
        payload = records.get(digest)
        if payload is None:
            problems.append(f"record {digest} for block {name!r} is missing from "
                            f"{RECORD_DIR}")
            continue
        # Re-hash rather than trust the key. ``load_record_store`` derives the key
        # by hashing, so a tampered FILE already lands under a different address
        # and shows up as missing — but this function also accepts a caller-built
        # map, and there a mismatched key would otherwise be believed.
        measured = sha256_bytes(canonical_bytes(payload))
        if measured != digest:
            problems.append(f"record for block {name!r} hashes to {measured} but "
                            f"was referenced as {digest}")
            continue
        if payload.get("block") != name:
            problems.append(f"record {digest} says it is block "
                            f"{payload.get('block')!r} but was referenced as {name!r}")
            continue
        out[name] = payload.get("value")
    return out, tuple(problems)


def load_semantic_snapshot(root: "Path | None" = None) -> dict:
    """The current generation in its V2 SEMANTIC shape, whatever the storage format.

    V69 M63 introduced the content-addressed V3 container. Callers that care
    about what the control plane MEANS — the candidates, the datasets, the
    defects — should read through here rather than json-loading the snapshot
    path, so a future container change does not reach them either.
    """
    root = root or REPO_ROOT
    current = json.loads((root / CURRENT_PATH).read_text(encoding="utf-8"))
    stored = json.loads(
        (root / current["latest_snapshot_path"]).read_text(encoding="utf-8"))
    if stored.get("schema_version") != CONTROL_PLANE_V3_SCHEMA_VERSION:
        return stored
    payload, problems = rehydrate_v3(stored, load_record_store(root / RECORD_DIR))
    if problems:
        raise SystemExit(f"S3N1_CONTROL_PLANE_UNREADABLE: rehydration: {problems[0]}")
    return payload


def load(report: Report) -> "ControlPlane | None":
    current_file = REPO_ROOT / CURRENT_PATH
    if not current_file.is_file():
        report.fail("CURRENT_POINTER", f"{CURRENT_PATH} is missing")
        return None
    current, current_raw = _load_json(current_file, CURRENT_PATH)

    rel = current.get("latest_snapshot_path")
    if not isinstance(rel, str) or not rel:
        report.fail("CURRENT_POINTER", "latest_snapshot_path is missing or not a string")
        return None
    snapshot_file = REPO_ROOT / rel
    if not _is_inside(snapshot_file, REPO_ROOT):
        report.fail("PATH_INTEGRITY", f"latest_snapshot_path escapes the repository: {rel}")
        return None
    if not snapshot_file.is_file():
        report.fail("CURRENT_POINTER", f"snapshot {rel} is missing")
        return None
    snapshot, snapshot_raw = _load_json(snapshot_file, rel)

    migration_file = REPO_ROOT / MIGRATION_MANIFEST_PATH
    migration: dict = {}
    if migration_file.is_file():
        migration, _ = _load_json(migration_file, MIGRATION_MANIFEST_PATH)
    else:
        report.fail("ARCHIVE_INTEGRITY",
                    f"{MIGRATION_MANIFEST_PATH} is missing; the archive's pinned digest "
                    f"has only one witness")
    # V69 M63 — a V3 generation is REHYDRATED here, before any check runs. Every
    # check below therefore reads the same V2 shape it always did, and the
    # format change is invisible to them by construction rather than by 60 edits.
    stored = snapshot
    records: dict = {}
    problems: tuple = ()
    if snapshot.get("schema_version") == CONTROL_PLANE_V3_SCHEMA_VERSION:
        records = load_record_store(REPO_ROOT / RECORD_DIR)
        snapshot, problems = rehydrate_v3(stored, records)
        for problem in problems:
            report.fail("SCHEMA", f"snapshot rehydration: {problem}")
    return ControlPlane(current, current_raw, snapshot, snapshot_raw, snapshot_file,
                        migration, snapshot_stored=stored, records=records,
                        rehydration_problems=problems)


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


# ── Checks ───────────────────────────────────────────────────────────────────────────
def check_schema(cp: ControlPlane, report: Report) -> None:
    """V1, V2, V28 — both documents strictly valid; unknown keys refused."""
    checks = [("current.json", current_schema(), cp.current),
              ("snapshot", snapshot_schema(), cp.snapshot)]
    if cp.is_v3:
        # BOTH contracts must hold: the stored container AND the semantics it
        # rehydrates to. Validating only one of them would let a well-formed V3
        # document carry a malformed V2 meaning, or the reverse.
        checks.append(("snapshot (stored v3)", snapshot_v3_schema(), cp.snapshot_stored))
    for label, schema, payload in checks:
        for problem in validate_against_schema(schema, payload):
            report.fail("SCHEMA", f"{label}: {problem}")

    # The published schema files must be the ones this verifier enforces. Two writable
    # copies of one contract is how they drift.
    for rel, builder in ((CURRENT_SCHEMA_PATH, current_schema),
                         (SNAPSHOT_SCHEMA_PATH, snapshot_schema),
                         (SNAPSHOT_V3_SCHEMA_PATH, snapshot_v3_schema),
                         (TRAIN_RECEIPT_SCHEMA_PATH, train_receipt_schema),
                         (EVAL_RECEIPT_SCHEMA_PATH, eval_receipt_schema),
                         (EVAL_RECEIPT_V2_SCHEMA_PATH, eval_receipt_v2_schema),
                         (EVAL_RECEIPT_V3_SCHEMA_PATH, eval_receipt_v3_schema),
                         (MEASUREMENT_WITNESS_SCHEMA_PATH,
                          measurement_witness_schema)):
        path = REPO_ROOT / rel
        if not path.is_file():
            report.fail("SCHEMA", f"{rel} is missing")
            continue
        if path.read_bytes() != canonical_bytes(builder()):
            report.fail("SCHEMA", f"{rel} differs from the schema the verifier enforces")

    # A second, independent opinion when the library is present. Absence is reported, not
    # treated as agreement.
    try:
        import jsonschema  # noqa: PLC0415  (optional second opinion, imported on demand)
    except ImportError:
        report.note("jsonschema not importable; the built-in strict validator is the "
                    "only opinion (it is the enforcing one either way)")
        return
    for label, schema, payload in (
            ("current.json", current_schema(), cp.current),
            ("snapshot", snapshot_schema(), cp.snapshot)):
        errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(payload),
                        key=lambda e: list(e.path))
        mine = validate_against_schema(schema, payload)
        if bool(errors) != bool(mine):
            report.fail("SCHEMA",
                        f"{label}: the two validators disagree — jsonschema "
                        f"{len(errors)} error(s), built-in {len(mine)}")
        for err in errors:
            report.fail("SCHEMA", f"{label} (jsonschema): {err.message}")


def check_current_pointer(cp: ControlPlane, report: Report) -> None:
    """V3, V26 — the pointer's digest is the snapshot's bytes, recomputed here."""
    measured = sha256_bytes(cp.snapshot_bytes)
    claimed = cp.current.get("latest_snapshot_sha256")
    if measured != claimed:
        report.fail("CURRENT_POINTER",
                    f"latest_snapshot_sha256 {claimed} != measured {measured}")
    if cp.current.get("state_generation") != cp.snapshot.get("state_generation"):
        report.fail("CURRENT_POINTER",
                    f"generation {cp.current.get('state_generation')} in the pointer != "
                    f"{cp.snapshot.get('state_generation')} in the snapshot")
    if cp.current.get("subject_state_commit") != cp.snapshot.get("subject_state_commit"):
        report.fail("CURRENT_POINTER", "subject_state_commit differs between the pointer "
                                       "and the snapshot")
    stored_version = (cp.snapshot_stored or cp.snapshot).get("schema_version")
    if cp.current.get("schema_version") != stored_version:
        report.fail("CURRENT_POINTER", "schema_version differs between the two documents")

    # The snapshot on disk must already BE in canonical form, so the digest above is the
    # digest of the file a human can read.
    # Measured against the document AS STORED. Under V3 ``cp.snapshot`` is the
    # rehydrated V2 shape, which is deliberately NOT what is on disk; comparing
    # against that would report every V3 generation as non-canonical.
    if cp.snapshot_bytes != canonical_bytes(cp.snapshot_stored or cp.snapshot):
        report.fail("SNAPSHOT_CHAIN",
                    "the snapshot file is not in canonical serialization; its digest "
                    "would depend on how it was written")
    if cp.current_bytes != canonical_bytes(cp.current):
        report.fail("CURRENT_POINTER", "current.json is not in canonical serialization")


def check_snapshot_chain(cp: ControlPlane, report: Report) -> None:
    """V4, V5 + one-writer and monotonicity — the append-only hash chain."""
    directory = REPO_ROOT / SNAPSHOT_DIR
    if not directory.is_dir():
        report.fail("SNAPSHOT_CHAIN", f"{SNAPSHOT_DIR} is missing")
        return
    files = sorted(p for p in directory.iterdir() if p.suffix == ".json")
    if not files:
        report.fail("SNAPSHOT_CHAIN", f"{SNAPSHOT_DIR} holds no snapshot")
        return

    seen: dict[int, Path] = {}
    payloads: dict[int, tuple[dict, bytes]] = {}
    for path in files:
        stem = path.name.split("-", 1)[0]
        if not re.fullmatch(r"[0-9]{4}", stem):
            report.fail("SNAPSHOT_CHAIN",
                        f"{path.name}: filename must start with a 4-digit generation")
            continue
        payload, raw = _load_json(path, path.name)
        generation = payload.get("state_generation")
        if generation != int(stem):
            report.fail("SNAPSHOT_CHAIN",
                        f"{path.name}: filename says generation {int(stem)}, payload "
                        f"says {generation}")
            continue
        if generation in seen:
            # Two agents produced the same generation. There is no last-write-wins and no
            # automatic merge: this is the optimistic-concurrency failure, and it stops.
            report.fail("SNAPSHOT_CHAIN",
                        f"generation {generation} is claimed by both "
                        f"{seen[generation].name} and {path.name}; ONE WRITER PER "
                        f"GENERATION")
            continue
        seen[generation] = path
        payloads[generation] = (payload, raw)

    generations = sorted(seen)
    if generations and generations[0] != 1:
        report.fail("SNAPSHOT_CHAIN", f"the chain starts at {generations[0]}, not 1")
    for previous, nxt in zip(generations, generations[1:]):
        if nxt != previous + 1:
            report.fail("SNAPSHOT_CHAIN",
                        f"generation jumps {previous} -> {nxt}; it must increment by 1")

    for generation in generations:
        payload, _ = payloads[generation]
        parent = payload.get("parent_snapshot_sha256")
        if generation == 1:
            if parent is not None:
                report.fail("SNAPSHOT_CHAIN",
                            "the genesis snapshot must have parent_snapshot_sha256 null")
            continue
        if generation - 1 not in payloads:
            report.fail("SNAPSHOT_CHAIN",
                        f"generation {generation} has no generation {generation - 1} to "
                        f"descend from")
            continue
        expected = sha256_bytes(payloads[generation - 1][1])
        if parent != expected:
            report.fail("SNAPSHOT_CHAIN",
                        f"generation {generation}: parent_snapshot_sha256 {parent} != "
                        f"sha256 of generation {generation - 1} ({expected})")

    if generations and cp.current.get("state_generation") != generations[-1]:
        report.fail("SNAPSHOT_CHAIN",
                    f"current.json points at generation "
                    f"{cp.current.get('state_generation')} but the newest snapshot on "
                    f"disk is {generations[-1]}")

    # V34 - snapshot immutability. A superseded snapshot is never revised; if one had
    # been, its digest would no longer match what its successor recorded, which the
    # parent check above already catches. Recorded here so the intent is not lost.
    report.note(f"snapshot chain length {len(generations)}, newest generation "
                f"{generations[-1] if generations else 'none'}")


def check_archive(cp: ControlPlane, report: Report) -> None:
    """V6, V7 — the archive exists and its bytes are the pinned ones. No auto-repair."""
    declared = cp.snapshot.get("archive", {})
    rel = declared.get("path")
    if rel != ARCHIVE_PATH:
        report.fail("ARCHIVE_INTEGRITY",
                    f"the snapshot names archive {rel!r}, the verifier pins "
                    f"{ARCHIVE_PATH!r}")
        return
    path = REPO_ROOT / rel
    if not path.is_file():
        report.fail("ARCHIVE_INTEGRITY", f"{rel} is missing")
        return

    measured = sha256_file(path)
    measured_bytes = path.stat().st_size
    if measured != declared.get("sha256"):
        report.fail("ARCHIVE_INTEGRITY",
                    f"{rel}: sha256 {measured} != the snapshot's pinned "
                    f"{declared.get('sha256')}. DO NOT update the expected hash — the "
                    f"remediation is operator review under a control-plane recovery "
                    f"milestone")
    if measured_bytes != declared.get("bytes"):
        report.fail("ARCHIVE_INTEGRITY",
                    f"{rel}: {measured_bytes} bytes != the snapshot's "
                    f"{declared.get('bytes')}")

    # Second witness. The migration manifest recorded the same digest at migration time;
    # a single writable value can drift silently, two compared against the bytes cannot.
    pinned = cp.migration.get("archive_sha256")
    if cp.migration and pinned != measured:
        report.fail("ARCHIVE_INTEGRITY",
                    f"{MIGRATION_MANIFEST_PATH} pins {pinned}, the bytes hash to "
                    f"{measured}")
    if cp.migration and pinned != declared.get("sha256"):
        report.fail("ARCHIVE_INTEGRITY",
                    "the migration manifest and the snapshot disagree about the archive "
                    "digest")
    if cp.migration.get("source_progress_sha256") != measured:
        report.fail("ARCHIVE_INTEGRITY",
                    "the migration manifest's source_progress_sha256 is not the archive's "
                    "digest; the archive is supposed to be a byte-for-byte copy")


def check_paths(cp: ControlPlane, report: Report) -> None:
    """V8, V9 — every control-plane file is a regular tracked file, never a symlink."""
    required = [ARCHIVE_PATH, CURRENT_PATH, HISTORY_INDEX_PATH, PROGRESS_PATH,
                CURRENT_SCHEMA_PATH, SNAPSHOT_SCHEMA_PATH, MIGRATION_MANIFEST_PATH,
                VERIFIER_PATH, str(cp.snapshot_path.relative_to(REPO_ROOT))]
    code, tracked_out = _git("ls-files", "-z", "--", *required)
    tracked = set(tracked_out.split("\0")) if code == 0 else set()
    if code != 0:
        report.fail("PATH_INTEGRITY", "git ls-files failed; tracking cannot be verified")

    for rel in required:
        path = REPO_ROOT / rel
        if path.is_symlink():
            report.fail("PATH_INTEGRITY",
                        f"{rel} is a symlink; a control-plane file must be the bytes it "
                        f"claims to be, not a redirection")
            continue
        if not path.is_file():
            report.fail("PATH_INTEGRITY", f"{rel} is not a regular file")
            continue
        if code == 0 and rel not in tracked:
            report.fail("PATH_INTEGRITY",
                        f"{rel} is not tracked by Git; an untracked control-plane file "
                        f"has no history and no second witness")

    # No parent directory of the state tree may be a symlink either.
    for rel in (STATE_DIR, SNAPSHOT_DIR, SCHEMA_DIR, MIGRATION_DIR,
                "jarvis/docs/m62", "jarvis/docs/m62/history"):
        path = REPO_ROOT / rel
        if path.is_symlink():
            report.fail("PATH_INTEGRITY", f"{rel} is a symlinked directory")

    # Executable bits: a data file that is executable is a surprise waiting to happen.
    for rel in (CURRENT_PATH, ARCHIVE_PATH, CURRENT_SCHEMA_PATH, SNAPSHOT_SCHEMA_PATH,
                MIGRATION_MANIFEST_PATH, PROGRESS_PATH,
                str(cp.snapshot_path.relative_to(REPO_ROOT))):
        path = REPO_ROOT / rel
        if path.is_file() and os.access(path, os.X_OK):
            report.fail("PATH_INTEGRITY", f"{rel} carries an executable bit")


def check_git_authority(cp: ControlPlane, report: Report) -> None:
    """V10-V13 — the subject commit exists, HEAD descends from it, refs are as declared."""
    subject = cp.snapshot.get("subject_state_commit", "")
    code, kind = _git("cat-file", "-t", subject)
    if code != 0 or kind != "commit":
        report.fail("GIT_AUTHORITY",
                    f"subject_state_commit {subject} is not a commit in this repository")
        return

    code, head = _git("rev-parse", "HEAD")
    if code != 0:
        report.fail("GIT_AUTHORITY", "HEAD could not be resolved")
        return
    code, _ = _git("merge-base", "--is-ancestor", subject, head)
    if code != 0:
        report.fail("GIT_AUTHORITY",
                    f"HEAD {head} does not descend from subject_state_commit {subject}; "
                    f"the recorded state describes a commit this tree is not built on")

    declared_branch = cp.snapshot.get("project", {}).get("branch")
    code, branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if code == 0 and branch != "HEAD" and branch != declared_branch:
        report.fail("GIT_AUTHORITY",
                    f"on branch {branch!r}, the snapshot declares {declared_branch!r}")

    declared_master = cp.snapshot.get("project", {}).get("master_commit")
    resolved = None
    for ref in ("refs/remotes/origin/master", "refs/heads/master"):
        code, value = _git("rev-parse", "--verify", "--quiet", ref)
        if code == 0 and value:
            resolved = (ref, value)
            break
    if resolved is None:
        # Fail closed: master is one of the declared invariants, and "the ref was not
        # available" is not evidence that it is untouched.
        report.fail("GIT_AUTHORITY",
                    "neither origin/master nor master resolves, so the declared master "
                    "commit cannot be checked")
    elif resolved[1] != declared_master:
        report.fail("GIT_AUTHORITY",
                    f"{resolved[0]} is {resolved[1]}, the snapshot declares "
                    f"{declared_master}. M62 must not move master")
    else:
        report.note(f"master verified against {resolved[0]}")


def check_stale_state(cp: ControlPlane, report: Report) -> None:
    """The stale-state detector, and an honest statement of what it cannot see.

    ENFORCED: no tracked state-bearing production path may change between
    ``subject_state_commit`` and HEAD without the snapshot moving with it.

    NOT DETECTABLE: a live training or evaluation run writes only gitignored runtime
    artefacts plus documentation, so this detector alone cannot prove that no candidate
    was trained since the snapshot was written. That limitation is recorded rather than
    papered over; the discipline that closes it is the milestone-close rule that every
    state-bearing milestone writes a new generation.
    """
    subject = cp.snapshot.get("subject_state_commit", "")
    code, out = _git("diff", "--name-only", f"{subject}..HEAD")
    if code != 0:
        report.fail("STALE_STATE",
                    "the subject..HEAD diff could not be computed, so staleness is "
                    "UNKNOWN rather than clean")
        return
    changed = [line for line in out.splitlines() if line]
    offenders = sorted(
        path for path in changed
        if any(path == p or path.startswith(p) for p in STATE_BEARING_PRODUCTION))
    if offenders:
        report.fail("STALE_STATE",
                    f"{len(offenders)} state-bearing production path(s) changed since "
                    f"{subject[:7]} without a new state generation: {offenders[:5]}")
    report.note(f"{len(changed)} tracked path(s) changed since the subject commit; "
                f"{len(offenders)} of them state-bearing")
    report.note("stale-state detection is PARTIAL: gitignored runtime artefacts "
                "(adapters, generations) are outside Git and cannot be diffed")

    # PROGRESS.md must cite the pointer it is bootstrapped alongside. Doc/state drift is
    # the other half of staleness and this half IS enforceable.
    progress = (REPO_ROOT / PROGRESS_PATH).read_text(encoding="utf-8")
    for needle, label in (
            (cp.current["latest_snapshot_sha256"], "the snapshot digest"),
            (cp.current["latest_snapshot_path"], "the snapshot path"),
            (cp.snapshot["subject_state_commit"], "the subject commit"),
            (cp.snapshot["archive"]["sha256"], "the archive digest")):
        if needle not in progress:
            report.fail("STALE_STATE",
                        f"PROGRESS.md does not cite {label} ({needle[:16]}...); the "
                        f"human-readable and machine-readable planes have drifted")


def check_dataset_state(cp: ControlPlane, report: Report) -> None:
    """V15-V17, V22, V30 — dataset vocabulary, the frozen identities, and the firewall."""
    datasets = cp.snapshot.get("datasets", [])
    by_key = {}
    for entry in datasets:
        key = f"{entry.get('dataset_id')} {entry.get('version')}"
        if key in by_key:
            report.fail("DATASET_STATE", f"duplicate dataset identity {key}")
        by_key[key] = entry
        if entry.get("status") not in DATASET_STATES:
            report.fail("DATASET_STATE",
                        f"{key}: status {entry.get('status')!r} is not a recognised "
                        f"dataset state")

    for key, (status, manifest) in FROZEN_DATASETS.items():
        entry = by_key.get(key)
        if entry is None:
            report.fail("DATASET_STATE", f"{key} is absent from the snapshot")
            continue
        if entry.get("status") != status:
            report.fail("DATASET_STATE",
                        f"{key}: status {entry.get('status')!r}, the sealed milestone "
                        f"authority says {status!r}")
        if entry.get("manifest_hash") != manifest:
            report.fail("DATASET_STATE",
                        f"{key}: manifest_hash {entry.get('manifest_hash')} != the frozen "
                        f"{manifest}")

    v4 = by_key.get("m62-defensive-eval v4", {})
    if v4.get("parent_manifest_hash") != FROZEN_DATASETS["m62-defensive-eval v3"][1]:
        report.fail("DATASET_STATE",
                    "eval-v4's parent is not eval-v3's frozen manifest; the D34 lineage "
                    "rule is that a parent is DECLARED, never discovered")
    if v4.get("pack_hash") != EVAL_V4_PACK_HASH:
        report.fail("DATASET_STATE",
                    f"eval-v4's pack_hash {v4.get('pack_hash')} != the frozen S3N value "
                    f"{EVAL_V4_PACK_HASH}")
    if v4.get("task_count") != 36:
        report.fail("DATASET_STATE", f"eval-v4 declares {v4.get('task_count')} tasks, not 36")

    v5 = by_key.get("m62-defensive-eval v5", {})
    if v5.get("parent_manifest_hash") != FROZEN_DATASETS["m62-defensive-eval v4"][1]:
        report.fail("DATASET_STATE",
                    "eval-v5's parent is not eval-v4's frozen manifest; the D34 lineage "
                    "rule is that a parent is DECLARED, never discovered, and a spent "
                    "parent is still a parent")
    if v5.get("pack_hash") != EVAL_V5_PACK_HASH:
        report.fail("DATASET_STATE",
                    f"eval-v5's pack_hash {v5.get('pack_hash')} != the frozen S3S value "
                    f"{EVAL_V5_PACK_HASH}")
    if v5.get("task_count") != 36:
        report.fail("DATASET_STATE", f"eval-v5 declares {v5.get('task_count')} tasks, not 36")

    v6 = by_key.get("m62-defensive-eval v6", {})
    if v6:
        if v6.get("parent_manifest_hash") != FROZEN_DATASETS["m62-defensive-eval v5"][1]:
            report.fail("DATASET_STATE",
                        "eval-v6's parent is not eval-v5's frozen manifest; the D34 "
                        "lineage rule is that a parent is DECLARED, never discovered, and "
                        "a RETIRED parent is still a parent -- retirement rules on what "
                        "may be measured against, not on where a corpus came from")
        if v6.get("pack_hash") != EVAL_V6_PACK_HASH:
            report.fail("DATASET_STATE",
                        f"eval-v6's pack_hash {v6.get('pack_hash')} != the frozen S3X.1 "
                        f"value {EVAL_V6_PACK_HASH}")
        if v6.get("task_count") != 36:
            report.fail("DATASET_STATE",
                        f"eval-v6 declares {v6.get('task_count')} tasks, not 36")

    for entry in datasets:
        role = entry.get("role")
        if role == "EVALUATION_HOLDOUT" and entry.get("status") == "USED_IMMUTABLE":
            if not entry.get("spent_by"):
                report.fail("DATASET_STATE",
                            f"{entry.get('dataset_id')} {entry.get('version')} is "
                            f"USED_IMMUTABLE but names nothing that spent it")
        if entry.get("status") == "FROZEN_UNUSED" and entry.get("spent_by"):
            report.fail("DATASET_STATE",
                        f"{entry.get('dataset_id')} {entry.get('version')} is "
                        f"FROZEN_UNUSED yet names something that spent it")


def check_candidate_state(cp: ControlPlane, report: Report) -> None:
    """V14, V18, V19, V29 — candidate vocabulary, the sealed verdicts, evidence shape."""
    candidates = cp.snapshot.get("candidates", [])
    by_id = {}
    for entry in candidates:
        cid = entry.get("candidate_id")
        if cid in by_id:
            report.fail("CANDIDATE_STATE", f"duplicate candidate identity {cid}")
        by_id[cid] = entry
        status = entry.get("status")
        if status not in CANDIDATE_STATES:
            report.fail("CANDIDATE_STATE",
                        f"{cid}: status {status!r} is not a recognised candidate state")
            continue
        if status in UNWITNESSABLE_CANDIDATE_STATES:
            report.fail("CANDIDATE_STATE",
                        f"{cid}: {status} cannot be witnessed by any artefact in this "
                        f"repository — no promotion mechanism exists, and the control "
                        f"plane may not assert one")
        if status == "NOT_CREATED":
            for field_name in ("adapter_sha256", "adapter_manifest_hash",
                               "evaluation_corpus", "training_corpus",
                               "base_model_revision"):
                if entry.get(field_name):
                    report.fail("CANDIDATE_STATE",
                                f"{cid}: NOT_CREATED yet carries {field_name}")
        if status == "DESIGNED_UNTRAINED":
            # Designed means: a configuration exists and NOTHING ELSE does. The two
            # adapter fields and the evaluation corpus must be absent, because weights
            # and a measurement are exactly what this state asserts have not happened.
            for field_name in ("adapter_sha256", "adapter_manifest_hash",
                               "evaluation_corpus"):
                if entry.get(field_name):
                    report.fail("CANDIDATE_STATE",
                                f"{cid}: DESIGNED_UNTRAINED yet carries {field_name}; a "
                                f"designed candidate has a configuration and no weights")
            for field_name in ("training_corpus", "base_model_revision", "evidence"):
                if not entry.get(field_name):
                    report.fail("CANDIDATE_STATE",
                                f"{cid}: DESIGNED_UNTRAINED without {field_name}; the "
                                f"state claims a design, so the design must be nameable")
        if status == "TRAINED_UNEVALUATED":
            # Trained means: weights exist, and a measurement does NOT. The adapter
            # digests and the receipt are required because they are what the state
            # asserts has happened; the evaluation corpus is refused because it is what
            # the state asserts has NOT. A candidate that carries an exam has taken one.
            for field_name in ("adapter_sha256", "adapter_manifest_hash",
                               "training_corpus", "base_model_revision", "evidence",
                               "training_receipt"):
                if not entry.get(field_name):
                    report.fail("CANDIDATE_STATE",
                                f"{cid}: TRAINED_UNEVALUATED without {field_name}; a "
                                f"trained candidate has weights and evidence that it "
                                f"earned them")
            if entry.get("evaluation_corpus"):
                report.fail("CANDIDATE_STATE",
                            f"{cid}: TRAINED_UNEVALUATED yet names an evaluation "
                            f"corpus; the state's whole content is that no held-out "
                            f"material has been spent on it")

        if status.startswith("EVALUATED_"):
            if not entry.get("evaluation_corpus"):
                report.fail("CANDIDATE_STATE",
                            f"{cid}: {status} without an evaluation corpus")
            if not entry.get("adapter_sha256"):
                report.fail("CANDIDATE_STATE",
                            f"{cid}: {status} without an adapter digest")
            if not entry.get("evidence"):
                report.fail("CANDIDATE_STATE",
                            f"{cid}: {status} without a deep evidence pointer")

    for cid, (status, adapter) in FROZEN_CANDIDATES.items():
        entry = by_id.get(cid)
        if entry is None:
            report.fail("CANDIDATE_STATE", f"{cid} is absent from the snapshot")
            continue
        if entry.get("status") != status:
            report.fail("CANDIDATE_STATE",
                        f"{cid}: status {entry.get('status')!r}, the sealed milestone "
                        f"authority says {status!r}")
        if entry.get("adapter_sha256") != adapter:
            report.fail("CANDIDATE_STATE",
                        f"{cid}: adapter_sha256 {entry.get('adapter_sha256')} != "
                        f"{adapter}")

    # V29 - the transition table, applied against the parent generation when one exists.
    parent = _parent_snapshot(cp)
    if parent is not None:
        # Resolved by ORDINAL, not by candidate_id. A candidate may legitimately be
        # RENAMED once -- generation 1 carried the placeholder `candidate-003` because
        # naming it was a design decision it was forbidden to make -- and keying this on
        # the id would make that rename a way to walk past the transition table
        # unchecked: `previous.get(new_id)` returns None, `before is None` skips, and
        # NOT_CREATED -> anything would pass silently. The ordinal is the stable lineage.
        previous_by_ordinal = {c.get("ordinal"): c for c in parent.get("candidates", [])}
        for cid, entry in by_id.items():
            ordinal = entry.get("ordinal")
            before_entry = previous_by_ordinal.get(ordinal)
            after = entry.get("status")
            if before_entry is None:
                # A candidate the parent generation never mentioned may only ENTER
                # at one of FRESH_ORDINAL_ENTRY_STATES. Otherwise a snapshot could mint
                # a fully-trained candidate at a fresh ordinal and face no transition
                # check at all. See that constant for why DESIGNED_UNTRAINED qualifies
                # and TRAINED_UNEVALUATED and every EVALUATED_* state do not.
                if after not in FRESH_ORDINAL_ENTRY_STATES:
                    report.fail("CANDIDATE_STATE",
                                f"{cid}: ordinal {ordinal} is absent from the parent "
                                f"generation and enters at {after}; a candidate the "
                                f"control plane has never seen may only enter as one of "
                                f"{list(FRESH_ORDINAL_ENTRY_STATES)}")
                continue
            before_id = before_entry.get("candidate_id")
            if before_id != cid:
                # The one legitimate rename is a recorded placeholder resolution.
                if CANDIDATE_IDENTITY_RESOLUTIONS.get(before_id) != cid:
                    report.fail("CANDIDATE_STATE",
                                f"ordinal {ordinal} was renamed {before_id} -> {cid}, "
                                f"which is not a recorded identity resolution. A "
                                f"candidate is not renamed to reuse its history")
            before = before_entry.get("status")
            if before == after:
                continue
            for problem in transition_problems(before, after, CANDIDATE_TRANSITIONS,
                                               "candidate"):
                report.fail("CANDIDATE_STATE", f"{cid}: {problem}")
        pdatasets = {f"{d.get('dataset_id')} {d.get('version')}": d.get("status")
                     for d in parent.get("datasets", [])}
        for entry in cp.snapshot.get("datasets", []):
            key = f"{entry.get('dataset_id')} {entry.get('version')}"
            before, after = pdatasets.get(key), entry.get("status")
            if before is None or before == after:
                continue
            for problem in transition_problems(before, after, DATASET_TRANSITIONS,
                                               "dataset"):
                report.fail("DATASET_STATE", f"{key}: {problem}")


def check_candidate_design(cp: ControlPlane, report: Report) -> None:
    """S3O — a DESIGNED_UNTRAINED claim is re-derived, never taken on the snapshot's word.

    This is the check that stops the control plane becoming self-fulfilling. The failure
    it exists to prevent is circular verification: the snapshot says
    ``DESIGNED_UNTRAINED``, a constant in this file says ``DESIGNED_UNTRAINED``, the two
    agree, and nothing has been verified at all. So the design is re-derived from the
    tracked production generator, and the snapshot must agree with THAT.

    Deliberately NOT checked here: ``config_hash``. It binds ``output_root_id``, a
    SHA-256 of a resolved absolute path, so it is a fact about one filesystem. The
    root-independent surfaces below identify the design everywhere; the root-bound digest
    is re-derived on the executing host at plan time, which is where it means something.

    No tokenizer, no weights, no network, no training framework: ``ChatRenderPolicy``
    takes the template digest as a STRING, so the render identity that IS candidate 003's
    experimental axis re-derives from the snapshot's own recorded digest.
    """
    # S3P widened this from DESIGNED_UNTRAINED to "has a design that must still hold".
    # Training does not retire the single-axis claim; it is the moment the claim starts
    # to describe real weights, so the re-derivation must survive the transition.
    designed = [c for c in cp.snapshot.get("candidates", [])
                if c.get("status") in ("DESIGNED_UNTRAINED", "TRAINED_UNEVALUATED")]
    if not designed:
        return

    if str(_PACKAGE_ROOT) not in sys.path:
        sys.path.insert(0, str(_PACKAGE_ROOT))
    try:
        # Imported as the PACKAGE module the rest of the repository imports. Importing
        # the same file by bare name would create a second module object with its own
        # copy of every constant, and the verifier would then be checking a generator
        # nothing else uses.
        from scripts import build_quality_training_config as generator
        from training_gym.evaluation.generation import ELIGIBILITY_REASONING_POLICY
        from training_gym.training.chat_render import ChatRenderPolicy, ReasoningPolicy
    except Exception as exc:
        report.fail("CANDIDATE_STATE",
                    f"the production candidate generator could not be imported ({exc}); "
                    f"a DESIGNED_UNTRAINED candidate is therefore UNVERIFIED, which is a "
                    f"failure and not a pass")
        return

    for entry in designed:
        cid = entry.get("candidate_id")
        key = next((k for k, spec in generator.CANDIDATES.items()
                    if spec.get("run_id") == cid), "")
        if not key:
            report.fail("CANDIDATE_STATE",
                        f"{cid}: DESIGNED_UNTRAINED, but no candidate in the production "
                        f"generator carries that run id. The state claims a design the "
                        f"repository cannot produce")
            continue
        spec = generator.CANDIDATES[key]

        # The corpus the design trains on must be the one the snapshot records, and the
        # snapshot's own dataset table must hold it as a real frozen identity.
        declared = str(entry.get("training_corpus") or "")
        if spec["dataset_version"] not in declared:
            report.fail("CANDIDATE_STATE",
                        f"{cid}: the generator trains it on "
                        f"{spec['dataset_version']}, the snapshot records "
                        f"{declared!r}")
        corpora = {f"{d.get('dataset_id')} {d.get('version')}"
                   for d in cp.snapshot.get("datasets", [])
                   if d.get("role") == "TRAINING_CORPUS"}
        if declared not in corpora:
            report.fail("CANDIDATE_STATE",
                        f"{cid}: training corpus {declared!r} is not a training dataset "
                        f"the snapshot carries an identity for")

        # The base model must be the control's, re-read from the generator constants.
        if generator.BASE_MODEL_ID != cp.snapshot.get("base_model", {}).get("model_id"):
            report.fail("CANDIDATE_STATE",
                        f"{cid}: the generator builds on {generator.BASE_MODEL_ID}, the "
                        f"snapshot records "
                        f"{cp.snapshot.get('base_model', {}).get('model_id')}")
        if generator.BASE_MODEL_REVISION != entry.get("base_model_revision"):
            report.fail("CANDIDATE_STATE",
                        f"{cid}: base revision {entry.get('base_model_revision')} is not "
                        f"the generator's {generator.BASE_MODEL_REVISION}")

        # THE AXIS. Resolved through the generator, and required to be the very object
        # evaluation generates under -- not a second enum member that spells the same.
        policy = generator.candidate_reasoning_policy(key)
        if policy is not ReasoningPolicy.DISABLED:
            report.fail("CANDIDATE_STATE",
                        f"{cid}: the generator renders it under {policy}, not DISABLED. "
                        f"The preregistered S3O axis is MODEL_DEFAULT -> DISABLED")
        if policy is not ELIGIBILITY_REASONING_POLICY:
            report.fail("CANDIDATE_STATE",
                        f"{cid}: its training reasoning policy is not the same object "
                        f"evaluation generates under; train/eval parity is the point")

        # THE CONTROL must not have moved with it, or there is no experiment.
        control = generator.candidate_reasoning_policy(CANDIDATE_CONTROL_KEY)
        if control is not ReasoningPolicy.MODEL_DEFAULT:
            report.fail("CANDIDATE_STATE",
                        f"the control candidate {CANDIDATE_CONTROL_KEY} renders under "
                        f"{control}, not the legacy MODEL_DEFAULT it actually trained "
                        f"under; the comparison is no longer controlled")

        # ONE AXIS. Every designed candidate declares, in the production generator, which
        # earlier candidate it is the experiment against and which dials that experiment
        # is allowed to move. The declaration is CHECKED here, not read: the generator's
        # own refusal runs first, and the dial set it computes must then equal the one it
        # declares.
        #
        # S3U widened this from a single equality to a declared relation. The equality it
        # replaced -- "candidate 003's option key IS candidate 002's" -- is not weakened:
        # an empty declared dial set still means shared BY KEY, and `verify_single_axis`
        # refuses two options whose numbers merely agree. What is new is the other shape,
        # a DERIVED option that may move exactly the dials it names, which is what a
        # learning-rate experiment is.
        relation = generator.CANDIDATE_SINGLE_AXIS.get(key)
        if relation is None:
            report.fail("CANDIDATE_STATE",
                        f"{cid}: the generator declares no single-axis relation for it, "
                        f"so 'exactly one thing changed' is an unchecked claim. A "
                        f"designed candidate names the candidate it is an experiment "
                        f"against")
        else:
            reference_key, declared = relation
            try:
                generator.verify_single_axis(key)
                moved = generator.single_axis_diff(key)
            except Exception as exc:
                report.fail("CANDIDATE_STATE",
                            f"{cid}: the production generator refuses its own design "
                            f"({exc})")
            else:
                if moved != declared:
                    report.fail("CANDIDATE_STATE",
                                f"{cid}: dial(s) {sorted(moved)} moved against candidate "
                                f"{reference_key}, but the declared single axis is "
                                f"{sorted(declared)}; a second experimental axis has "
                                f"appeared")

        # The render identity that IS the axis, re-derived from the snapshot's own
        # template digest. String inputs only: no tokenizer is loaded.
        base = cp.snapshot.get("base_model", {})
        def _render(reasoning: object) -> str:
            return ChatRenderPolicy(
                tokenizer_id=base.get("model_id", ""),
                tokenizer_revision=base.get("revision", ""),
                chat_template_hash=base.get("chat_template_digest", ""),
                reasoning_policy=reasoning,
                add_generation_prompt=True, tokenize=True).render_policy_hash()
        try:
            if _render(policy) == _render(ReasoningPolicy.MODEL_DEFAULT):
                report.fail("CANDIDATE_STATE",
                            f"{cid}: its render identity equals the legacy one, so the "
                            f"axis moved nothing")
        except Exception as exc:
            report.fail("CANDIDATE_STATE",
                        f"{cid}: the render identity could not be re-derived ({exc})")

        # The deep evidence must exist and be tracked. A pointer to a file Git does not
        # carry is a pointer to something a reader cannot audit.
        pointer = str(entry.get("evidence") or "")
        evidence_file = REPO_ROOT / pointer
        if not pointer or not evidence_file.is_file():
            report.fail("CANDIDATE_STATE",
                        f"{cid}: deep evidence {pointer!r} is not a file in this tree")
        else:
            code, out = _git("ls-files", "--error-unmatch", "--", pointer)
            if code != 0 or not out:
                report.fail("CANDIDATE_STATE",
                            f"{cid}: deep evidence {pointer} is untracked")

        # DESIGNED means no weights and no completed run. Both are gitignored runtime
        # artefacts, so their absence is checked on disk and the limit is stated.
        #
        # These three checks are the ONLY ones in this function that are specific to
        # DESIGNED_UNTRAINED. Everything above -- the corpus, the base model, the axis,
        # the control, the single-option guard, the render identity and the tracked deep
        # evidence -- re-derives the DESIGN, which a trained candidate still has and must
        # still satisfy. A candidate that trained and then quietly grew a second axis
        # would otherwise stop being checked at the moment it started to matter.
        if entry.get("status") == "DESIGNED_UNTRAINED":
            for runtime_dir in (REPO_ROOT / "jarvis" / "training_runs" / "runs" / cid,
                                REPO_ROOT / "jarvis" / "training_adapters" / cid):
                if runtime_dir.exists():
                    report.fail("CANDIDATE_STATE",
                                f"{cid}: DESIGNED_UNTRAINED, but {runtime_dir.name} "
                                f"exists as a runtime artefact; a designed candidate "
                                f"has no run")
            ledger = REPO_ROOT / "jarvis" / "training_runs" / "training_runs.jsonl"
            if ledger.is_file() and cid in ledger.read_text(encoding="utf-8"):
                report.fail("CANDIDATE_STATE",
                            f"{cid}: DESIGNED_UNTRAINED, but the run ledger already "
                            f"names it; a plan was consumed for this identity")
        report.note(f"{cid}: {entry.get('status')} design re-derived from the production "
                    f"generator (option {generator.CANDIDATE_OPTION.get(key)}, corpus "
                    f"{spec['dataset_version']}, reasoning {policy.value}); runtime "
                    f"presence is host-local and inherits the PARTIAL stale-state limit")


def check_training_receipt(cp: ControlPlane, report: Report) -> None:
    """S3P — a TRAINED_UNEVALUATED claim is backed by portable evidence, or refused.

    THE FAILURE THIS EXISTS TO PREVENT
    ----------------------------------
    The snapshot says ``TRAINED_UNEVALUATED``. A constant in this file says
    ``TRAINED_UNEVALUATED``. They agree, the verifier prints PASS, and **nothing has
    been verified** — two writable surfaces agreeing is not evidence, it is a rumour
    with a checksum. So the trained claim is refused outright unless a tracked,
    root-independent receipt independently establishes it, and the receipt must agree
    with the snapshot, with the production generator and with the sealed structural
    control.

    WHY A RECEIPT AND NOT THE RUN DIRECTORY
    ---------------------------------------
    The adapter, its manifest and the run ledger are gitignored runtime artefacts. They
    are the right home for weights and the wrong home for history: a fresh clone has
    none of them, so a control plane resting on them could never be audited anywhere
    else. The receipt is the part that travels (S3P §49). Runtime presence is therefore
    reported as an observation and is deliberately NOT required — a trained candidate
    stays trained after its runtime tree is deleted.

    This check loads no model, no tokenizer and no weights: the render identity that IS
    candidate 003's experimental axis re-derives from strings.
    """
    trained = [c for c in cp.snapshot.get("candidates", [])
               if c.get("status") == "TRAINED_UNEVALUATED"]
    if not trained:
        return

    if str(_PACKAGE_ROOT) not in sys.path:
        sys.path.insert(0, str(_PACKAGE_ROOT))
    try:
        from scripts import build_quality_training_config as generator
        from training_gym.training.chat_render import ChatRenderPolicy, ReasoningPolicy
    except Exception as exc:
        report.fail("TRAINING_RECEIPT",
                    f"the production generator could not be imported ({exc}); a "
                    f"TRAINED_UNEVALUATED candidate is therefore UNVERIFIED, which is a "
                    f"failure and not a pass")
        return

    for entry in trained:
        cid = entry.get("candidate_id")
        pointer = str(entry.get("training_receipt") or "")
        if not pointer:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: TRAINED_UNEVALUATED with no receipt pointer. The state "
                        f"claims a completed training run and offers nothing a reader "
                        f"could check it against")
            continue
        path = REPO_ROOT / pointer
        if path.is_symlink() or not path.is_file():
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: training receipt {pointer!r} is not a regular file")
            continue
        code, tracked = _git("ls-files", "--error-unmatch", "--", pointer)
        if code != 0 or not tracked:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: training receipt {pointer} is untracked; evidence Git "
                        f"does not carry has no history and no second witness")

        raw = path.read_bytes()
        try:
            receipt = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            report.fail("TRAINING_RECEIPT", f"{cid}: receipt is unreadable ({exc})")
            continue
        for problem in validate_against_schema(train_receipt_schema(), receipt):
            report.fail("TRAINING_RECEIPT", f"{cid}: receipt {problem}")

        # The receipt is held to the same content rules as every other control-plane
        # surface: no token literal, no private path, no task material, ASCII only.
        text = raw.decode("utf-8", errors="replace")
        if TOKEN_LITERAL_RE.search(text):
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the receipt carries something shaped like a spendable "
                        f"plan token. A receipt proves an authority was spent; it never "
                        f"reproduces one")
        for match in PRIVATE_PATH_RE.findall(text):
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the receipt carries a private host path {match!r}")
        for symbol in FORBIDDEN_BODY_SYMBOLS:
            if symbol in text:
                report.fail("TRAINING_RECEIPT",
                            f"{cid}: the receipt references {symbol!r}, the "
                            f"eval-{body_symbol_version(symbol)} body source")
        for held_out, task_ids in HELD_OUT_TASK_IDS.items():
            named = sorted({tid for tid in task_ids if tid in text})
            if named:
                report.fail("TRAINING_RECEIPT",
                            f"{cid}: the receipt names eval-{held_out} task(s) "
                            f"{named[:4]}")
        try:
            text.encode("ascii")
        except UnicodeEncodeError:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the receipt is not ASCII, so its canonical bytes depend "
                        f"on an encoding choice")

        # ── the receipt must describe THIS candidate, not merely a trained one ──
        if receipt.get("candidate_id") != cid:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the receipt describes "
                        f"{receipt.get('candidate_id')!r}. A receipt for another run is "
                        f"not evidence about this one")
            continue

        key = next((k for k, spec in generator.CANDIDATES.items()
                    if spec.get("run_id") == cid), "")
        if not key:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: no candidate in the production generator carries that "
                        f"run id, so the trained claim describes a candidate this "
                        f"repository cannot re-derive")
            continue
        option = generator.OPTIONS[generator.CANDIDATE_OPTION[key]]

        base = cp.snapshot.get("base_model", {})
        receipt_base = receipt.get("base_model", {})
        for label, mine, theirs in (
                ("base model", receipt_base.get("model_id"), base.get("model_id")),
                ("base revision", receipt_base.get("revision"),
                 entry.get("base_model_revision")),
                ("chat template digest", receipt_base.get("chat_template_digest"),
                 base.get("chat_template_digest"))):
            if mine != theirs:
                report.fail("TRAINING_RECEIPT",
                            f"{cid}: the receipt's {label} {mine!r} is not the "
                            f"snapshot's {theirs!r}")
        if receipt_base.get("model_id") != generator.BASE_MODEL_ID:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the receipt trained on {receipt_base.get('model_id')!r},"
                        f" the generator builds on {generator.BASE_MODEL_ID!r}")
        if receipt_base.get("revision") != generator.BASE_MODEL_REVISION:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the receipt's revision is not the generator's pinned "
                        f"{generator.BASE_MODEL_REVISION}")

        # ── the corpus: the receipt, the snapshot and the frozen identity ──────
        dataset = receipt.get("training_dataset", {})
        declared = f"{dataset.get('dataset_id')} {dataset.get('version')}"
        if declared != str(entry.get("training_corpus") or ""):
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the receipt trained on {declared!r}, the snapshot "
                        f"records {entry.get('training_corpus')!r}")
        frozen = FROZEN_DATASETS.get(declared)
        if frozen is None:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: {declared!r} is not a dataset this control plane holds "
                        f"a sealed identity for")
        elif dataset.get("manifest_hash") != frozen[1]:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the receipt's corpus manifest "
                        f"{dataset.get('manifest_hash')} is not the sealed {frozen[1]}")
        if dataset.get("version") != generator.CANDIDATES[key]["dataset_version"]:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the generator trains it on "
                        f"{generator.CANDIDATES[key]['dataset_version']}, the receipt "
                        f"records {dataset.get('version')}")

        # ── S3V: the EXPORT it actually trained on, not merely the corpus it names ──
        # The manifest above identifies the dataset; these identify the material. Left
        # unbound, a receipt could name the sealed corpus while recording an export,
        # train shard or validation shard that nothing in the repository ever promoted.
        exports = FROZEN_TRAIN_EXPORTS.get(declared)
        if exports is None:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: {declared!r} has no sealed export identity, so the "
                        f"material this run trained on is unverifiable")
        else:
            for field, sealed in zip(("export_manifest_hash", "train_shard_hash",
                                      "validation_shard_hash"), exports):
                if dataset.get(field) != sealed:
                    report.fail("TRAINING_RECEIPT",
                                f"{cid}: the receipt's {field} {dataset.get(field)} is "
                                f"not the sealed {sealed}")

        # ── THE AXIS, re-derived and required to still be the only one ─────────
        policy = generator.candidate_reasoning_policy(key)
        representation = receipt.get("representation", {})
        if representation.get("reasoning_policy") != policy.value:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: it trained under "
                        f"{representation.get('reasoning_policy')!r}, the generator "
                        f"designs it as {policy.value!r}")
        if policy is not ReasoningPolicy.DISABLED:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the generator renders it under {policy}, not DISABLED")
        try:
            rendered = ChatRenderPolicy(
                tokenizer_id=base.get("model_id", ""),
                tokenizer_revision=base.get("revision", ""),
                chat_template_hash=base.get("chat_template_digest", ""),
                reasoning_policy=policy,
                add_generation_prompt=True, tokenize=True).render_policy_hash()
        except Exception as exc:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the render identity could not be re-derived ({exc})")
        else:
            if representation.get("chat_render_policy_hash") != rendered:
                report.fail("TRAINING_RECEIPT",
                            f"{cid}: it trained under render identity "
                            f"{representation.get('chat_render_policy_hash')}, which is "
                            f"not the {rendered} the design re-derives. The run did not "
                            f"execute the designed representation")

        # ── the authority: created once, consumed once, never reproduced ───────
        authority = receipt.get("authority", {})
        if authority.get("creations") != 1:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the receipt records {authority.get('creations')} "
                        f"authority creation(s); exactly one was authorised")
        if authority.get("consumptions") != 1:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the receipt records {authority.get('consumptions')} "
                        f"consumption(s); a single-use capability is spent exactly once")
        if authority.get("bound_plan_hash") != receipt.get("plan_hash"):
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the authority is bound to a different plan than the run "
                        f"executed")
        if receipt.get("plan_hash") not in receipt.get("ledger", {}).get(
                "plan_hashes", []):
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the ledger does not name the plan the run executed")

        # ── the execution: complete, terminal, and to the preregistered budget ──
        execution = receipt.get("execution", {})
        if execution.get("terminal_status") != "SUCCESS":
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the run's terminal status is "
                        f"{execution.get('terminal_status')!r}. A candidate is not "
                        f"trained by an attempt that did not succeed")
        if execution.get("interrupted") or not execution.get("completed"):
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the run did not complete uninterrupted")
        planned = option["max_steps"]
        if execution.get("optimizer_steps_planned") != planned:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the receipt plans "
                        f"{execution.get('optimizer_steps_planned')} optimizer steps, "
                        f"the design declares {planned}")
        if execution.get("optimizer_steps_completed") != planned:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: {execution.get('optimizer_steps_completed')} of "
                        f"{planned} optimizer steps completed. A short run is a "
                        f"different experiment, not this one")
        if execution.get("epochs_configured") != option["epochs"]:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the receipt configures "
                        f"{execution.get('epochs_configured')} epochs, the design "
                        f"declares {option['epochs']}")
        # S3V: the epochs it CONFIGURED were bound above; the epochs it COMPLETED were
        # not, so a run that stopped a full pass early could still call itself trained.
        completed_epochs = execution.get("epochs_completed")
        if (completed_epochs is None
                or not math.isfinite(float(completed_epochs))
                or float(completed_epochs) != float(option["epochs"])):
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: {completed_epochs} of {option['epochs']} epochs "
                        f"completed. A run that stopped short trained a different "
                        f"candidate than the one designed")
        # S3V: the ledger tells one story or the receipt is not evidence of it. Exactly
        # one start and one terminal event, under exactly one plan.
        events = receipt.get("ledger", {}).get("events", {})
        if events.get("started") != 1:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the ledger records {events.get('started')} start(s); a "
                        f"single-use authority starts exactly one run, and a second "
                        f"start is a retry nothing authorised")
        if events.get("completed") != 1:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the ledger records {events.get('completed')} completion "
                        f"event(s) for a run the receipt calls SUCCESS")
        if len(set(receipt.get("ledger", {}).get("plan_hashes", []))) != 1:
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the ledger names more than one plan for this identity")
        if not execution.get("final_validation_present"):
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: no closing validation pass is recorded (D31)")
        losses = list(execution.get("validation_losses") or [])
        if len(losses) != execution.get("validation_evaluations"):
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: {len(losses)} validation losses against "
                        f"{execution.get('validation_evaluations')} evaluations")
        for value in [*losses, execution.get("train_loss"),
                      execution.get("final_validation_loss")]:
            if value is None or not math.isfinite(float(value)):
                report.fail("TRAINING_RECEIPT",
                            f"{cid}: a recorded metric is not finite; a run whose "
                            f"numbers are not numbers did not train anything")

        # ── the adapter: identities present, and STRUCTURALLY the control's ────
        adapter = receipt.get("adapter", {})
        for field_name, snapshot_field in (("sha256", "adapter_sha256"),
                                           ("manifest_hash", "adapter_manifest_hash")):
            if not adapter.get(field_name):
                report.fail("TRAINING_RECEIPT",
                            f"{cid}: the receipt records no adapter {field_name}")
            elif adapter.get(field_name) != entry.get(snapshot_field):
                report.fail("TRAINING_RECEIPT",
                            f"{cid}: the receipt's adapter {field_name} "
                            f"{adapter.get(field_name)} is not the snapshot's "
                            f"{entry.get(snapshot_field)}")
        if not adapter.get("artifact_set_hash"):
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the receipt records no artifact-set hash")
        else:
            # S3V: presence was checked; IDENTITY was not, so the one digest that covers
            # the whole artefact set could be replaced freely. Sealed per candidate
            # because it cannot be re-derived without the gitignored run tree.
            sealed_set = FROZEN_ADAPTER_ARTIFACT_SETS.get(cid)
            if sealed_set is None:
                report.fail("TRAINING_RECEIPT",
                            f"{cid}: no sealed artifact-set hash is recorded for this "
                            f"candidate, so its artefact set is unverifiable")
            elif adapter.get("artifact_set_hash") != sealed_set:
                report.fail("TRAINING_RECEIPT",
                            f"{cid}: the receipt's artifact-set hash "
                            f"{adapter.get('artifact_set_hash')} is not the sealed "
                            f"{sealed_set}")
        # S3V: the LoRA geometry the design declares, re-derived rather than pinned --
        # rank, alpha and dropout are the dials candidate 004's whole single-axis claim
        # rests on staying equal to candidate 003's.
        for field_name, dial in (("lora_rank", "lora_rank"),
                                 ("lora_alpha", "lora_alpha"),
                                 ("lora_dropout", "lora_dropout")):
            if adapter.get(field_name) != option[dial]:
                report.fail("TRAINING_RECEIPT",
                            f"{cid}: the adapter records {field_name} "
                            f"{adapter.get(field_name)}, the design declares "
                            f"{option[dial]}")
        # S3V: two parameter counts that must agree. A LoRA adapter's parameters ARE its
        # trainable parameters; a receipt where they diverge is describing two adapters.
        if adapter.get("adapter_parameter_count") != adapter.get("trainable_parameters"):
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: adapter_parameter_count "
                        f"{adapter.get('adapter_parameter_count')} != "
                        f"trainable_parameters {adapter.get('trainable_parameters')}")
        for field_name, expected in STRUCTURAL_ADAPTER_CONTROL.items():
            if adapter.get(field_name) != expected:
                report.fail("TRAINING_RECEIPT",
                            f"{cid}: adapter {field_name} is "
                            f"{adapter.get(field_name)}, the sealed structural control "
                            f"for this architecture is {expected}. Learned values "
                            f"differ between candidates; STRUCTURE does not")
        if set(adapter.get("target_modules") or ()) != set(
                STRUCTURAL_ADAPTER_TARGET_MODULES):
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: it adapted {sorted(adapter.get('target_modules') or ())},"
                        f" not the {CANDIDATE_003_LORA_SCOPE} projection set")

        # ── the verifiers the run was actually put through ─────────────────────
        verification = receipt.get("verification", {})
        if verification.get("completed_run_verifier") != "PASS":
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the completed-run verifier did not pass")
        if verification.get("adapter_verifier") != "valid":
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the adapter verifier returned "
                        f"{verification.get('adapter_verifier')!r}")
        if verification.get("checkpoint_directories"):
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the run wrote checkpoint directories, which the adapter "
                        f"artefact policy refuses (D16)")

        # ── still unevaluated, and the holdout still unspent ───────────────────
        holdout = receipt.get("holdout", {})
        if holdout.get("held_out_evaluation_runs") or holdout.get(
                "eval_authority_created") or holdout.get("evaluation_corpus"):
            report.fail("TRAINING_RECEIPT",
                        f"{cid}: the receipt records held-out evaluation, but the state "
                        f"is TRAINED_UNEVALUATED")

        # ── the commits this run stands on ────────────────────────────────────
        for label, commit in (("training_source_commit",
                               receipt.get("training_source_commit")),
                              ("design_commit", receipt.get("design_commit"))):
            code, kind = _git("cat-file", "-t", str(commit))
            if code != 0 or kind != "commit":
                report.fail("TRAINING_RECEIPT",
                            f"{cid}: receipt {label} {commit} is not a commit in this "
                            f"repository")

        runtime_tree = REPO_ROOT / "jarvis" / "training_runs" / "runs" / cid
        report.note(
            f"{cid}: TRAINED_UNEVALUATED backed by {pointer} — one authority created, "
            f"one consumed, {execution.get('optimizer_steps_completed')}/"
            f"{planned} optimizer steps, adapter {str(adapter.get('sha256'))[:8]}...; "
            f"runtime adapter present locally: "
            f"{'YES' if runtime_tree.is_dir() else 'NO'} (historical training is sealed "
            f"by the receipt and does not depend on it)")


def check_evaluation_receipt(cp: ControlPlane, report: Report) -> None:
    """S3Q.0 — an EVALUATED_* claim is backed by portable evidence, or refused.

    THE FAILURE THIS EXISTS TO PREVENT
    ----------------------------------
    Exactly the one ``check_training_receipt`` prevents, one door further in. The
    snapshot says ``EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW``; a constant in this file agrees;
    the verifier prints PASS; and nothing has been verified. Two writable surfaces
    agreeing is a rumour with a checksum, and here the rumour would be about the one
    irreversible act in the whole milestone — a fresh holdout having been spent.

    So a modern candidate's evaluated state is refused outright unless a tracked,
    root-independent receipt independently establishes it, and the receipt must bind the
    plan that was approved, the pack that was measured, the durable commit that spent the
    holdout, the terminal event, and an eligibility that AGREES with the state claimed.

    WHY CANDIDATES 001 AND 002 ARE EXEMPT
    -------------------------------------
    They were measured before this evidence form existed. Their sealed milestone
    documents are their authority. Synthesising a receipt for a run that never emitted
    one would be manufacturing evidence, which is strictly worse than declaring the gap —
    so the gap is declared, the set is closed, and nothing may be added to it.

    This check loads no model, opens no socket and reads no held-out material.
    """
    for entry in cp.snapshot.get("candidates", []):
        cid = str(entry.get("candidate_id") or "")
        status = str(entry.get("status") or "")
        pointer = str(entry.get("evaluation_receipt") or "")

        if status not in EVALUATED_CANDIDATE_STATES:
            # A candidate that has NOT been evaluated may not carry evaluation evidence:
            # a receipt beside TRAINED_UNEVALUATED would be evidence of the very thing
            # the state asserts has not happened.
            if pointer:
                report.fail("EVALUATION_RECEIPT",
                            f"{cid}: {status or 'no status'} yet carries an evaluation "
                            f"receipt. A candidate that has not been measured has no "
                            f"measurement to show")
            continue

        if cid in LEGACY_EVALUATION_CANDIDATES:
            report.note(
                f"{cid}: {status} predates the portable evaluation receipt (S3Q.0). Its "
                f"authority is its sealed milestone document; it is NOT retrofitted, and "
                f"the legacy set is closed")
            continue

        if not pointer:
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: {status} with no evaluation receipt pointer. The state "
                        f"claims a fresh holdout was spent on this candidate and offers "
                        f"nothing a reader could check it against")
            continue

        path = REPO_ROOT / pointer
        if path.is_symlink() or not path.is_file():
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: evaluation receipt {pointer!r} is not a regular file")
            continue
        code, tracked = _git("ls-files", "--error-unmatch", "--", pointer)
        if code != 0 or not tracked:
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: evaluation receipt {pointer} is untracked; evidence Git "
                        f"does not carry has no history and no second witness")

        raw = path.read_bytes()
        try:
            receipt = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            report.fail("EVALUATION_RECEIPT", f"{cid}: receipt is unreadable ({exc})")
            continue
        # S3Q.0.1. The version decides the contract. A candidate measured after the
        # modern receipt existed may not present a `.1` document: that would be evidence
        # deliberately written to the weaker contract, which makes the upgrade optional
        # and the audit worth what the weakest accepted form is worth.
        version = str(receipt.get("schema_version", ""))
        if version not in MODERN_EVAL_RECEIPT_VERSIONS:
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: receipt schema {version or '(absent)'!r} is not a "
                        f"modern evaluation receipt "
                        f"{sorted(MODERN_EVAL_RECEIPT_VERSIONS)}; every candidate "
                        f"measured after S3Q.0.1 produces one")
            continue
        # The VERSION decides the contract. `.2` and `.3` are both real, both tracked
        # and both satisfiable; validating a `.3` against `.2`'s shape would report the
        # repair as a violation.
        schema = (eval_receipt_v3_schema()
                  if version == EVAL_RECEIPT_V3_SCHEMA_VERSION
                  else eval_receipt_v2_schema())
        for problem in validate_against_schema(schema, receipt):
            report.fail("EVALUATION_RECEIPT", f"{cid}: receipt {problem}")

        # Held to every content rule the other control-plane surfaces are: no token
        # literal, no private path, no held-out material, ASCII only.
        text = raw.decode("utf-8", errors="replace")
        if TOKEN_LITERAL_RE.search(text):
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: the receipt carries something shaped like a spendable "
                        f"plan token. A receipt proves an authority was spent; it never "
                        f"reproduces one")
        for match in PRIVATE_PATH_RE.findall(text):
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: the receipt carries a private host path {match!r}")
        for symbol in FORBIDDEN_BODY_SYMBOLS:
            if symbol in text:
                report.fail("EVALUATION_RECEIPT",
                            f"{cid}: the receipt references {symbol!r}, the "
                            f"eval-{body_symbol_version(symbol)} body source")
        for held_out, task_ids in HELD_OUT_TASK_IDS.items():
            named = sorted({tid for tid in task_ids if tid in text})
            if named:
                report.fail("EVALUATION_RECEIPT",
                            f"{cid}: the receipt names eval-{held_out} task(s) "
                            f"{named[:4]}")
        # S3Q.0.2. `.1` and `.2` refuse non-ASCII, because that is the contract their
        # documents were written and hashed under. `.3` DEFINES its canonical bytes as
        # canonical JSON encoded UTF-8 instead, which closes the same ambiguity without
        # discarding legitimate production decision text -- a gate message correctly
        # typeset with U+2212 is the report's own evidence, not a formatting accident.
        if version in UTF8_CANONICAL_RECEIPT_VERSIONS:
            if receipt.get("canonical_encoding") != CANONICAL_RECEIPT_ENCODING:
                report.fail("EVALUATION_RECEIPT",
                            f"{cid}: the receipt does not state its canonical encoding, "
                            f"so its bytes depend on an unstated choice after all")
            if raw != canonical_bytes(receipt):
                report.fail("EVALUATION_RECEIPT",
                            f"{cid}: the bytes on disk are not the canonical bytes of "
                            f"the document they parse to; the file and the digest "
                            f"describe different receipts")
        else:
            try:
                text.encode("ascii")
            except UnicodeEncodeError:
                report.fail("EVALUATION_RECEIPT",
                            f"{cid}: the receipt is not ASCII, so its canonical bytes "
                            f"depend on an encoding choice")

        # ── the receipt must describe THIS candidate and THIS state ──────────
        candidate = receipt.get("candidate", {})
        if candidate.get("candidate_id") != cid:
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: the receipt describes "
                        f"{candidate.get('candidate_id')!r}. A receipt for another run "
                        f"is not evidence about this one")
            continue
        if candidate.get("status_claim") != status:
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: the snapshot claims {status} and the receipt's "
                        f"eligibility supports {candidate.get('status_claim')!r}. The "
                        f"receipt decides, not the snapshot")

        # ── the digest, re-derived from the bytes rather than trusted ────────
        body = {k: v for k, v in receipt.items() if k != "receipt_hash"}
        actual = sha256_bytes(canonical_bytes(body))
        if receipt.get("receipt_hash") != actual:
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: receipt_hash does not match the bytes; a receipt that "
                        f"can be edited without its digest moving can be edited to say "
                        f"anything")

        # ── the three durable events, each counted ───────────────────────────
        ledger = receipt.get("ledger", {})
        for field, label in (("plan_started_count", "plan-start"),
                             ("holdout_commit_count", "model-facing commit"),
                             ("terminal_count", "terminal")):
            if ledger.get(field) != 1:
                report.fail("EVALUATION_RECEIPT",
                            f"{cid}: the receipt binds {ledger.get(field)!r} {label} "
                            f"event(s); exactly one is what a single-use ceremony means")

        # ── the plan bound what was measured, and said it would run a model ──
        plan = receipt.get("plan", {})
        if not plan.get("binds_exact_pack_identity"):
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: the approved plan did not bind the exact pack identity "
                        f"that was measured")
        if not plan.get("performs_inference"):
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: the receipt describes a live evaluation under a plan "
                        f"that declared it would run no model")
        commit = receipt.get("holdout_commit", {})
        if commit.get("order_assignment_hash") != plan.get("order_assignment_hash"):
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: the model-facing commit and the approved plan disagree "
                        f"about the execution order")

        # ── the holdout named here must be the one the datasets section spent ─
        holdout = receipt.get("holdout", {})
        key = f"{holdout.get('dataset_id')} {holdout.get('dataset_version')}"
        spent = {f"{d.get('dataset_id')} {d.get('version')}": d
                 for d in cp.snapshot.get("datasets", [])}
        dataset = spent.get(key)
        if dataset is None:
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: the receipt names holdout {key!r}, which the control "
                        f"plane does not list")
        else:
            if dataset.get("status") != "USED_IMMUTABLE":
                report.fail("EVALUATION_RECEIPT",
                            f"{cid}: the receipt records that {key} was spent on this "
                            f"candidate while the control plane still calls it "
                            f"{dataset.get('status')!r}")
            if dataset.get("manifest_hash") != holdout.get("dataset_manifest_hash"):
                report.fail("EVALUATION_RECEIPT",
                            f"{cid}: the receipt and the control plane disagree about "
                            f"{key}'s manifest digest")
            if entry.get("evaluation_corpus") and \
                    holdout.get("dataset_id") not in str(entry.get("evaluation_corpus")):
                report.fail("EVALUATION_RECEIPT",
                            f"{cid}: the snapshot names evaluation corpus "
                            f"{entry.get('evaluation_corpus')!r} and the receipt "
                            f"measured {key}")

        # ── a receipt grants nothing ────────────────────────────────────────
        authority = receipt.get("authority", {})
        if authority.get("retry_authorized") or authority.get("token_literal_recorded") \
                or not authority.get("grants_no_further_authority"):
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: the receipt asserts an authority. Evidence of an "
                        f"operation never authorises another one")
        outcome = receipt.get("outcome", {})
        for flag in ("promotes_model", "activates_model", "mutates_model_registry"):
            if outcome.get(flag):
                report.fail("EVALUATION_RECEIPT",
                            f"{cid}: the receipt claims {flag}; no mechanism in this "
                            f"repository could have performed it")

        _check_modern_evaluation_receipt(cp, report, entry=entry, receipt=receipt,
                                         raw=raw, cid=cid)


def _check_modern_evaluation_receipt(cp: ControlPlane, report: Report, *, entry: dict,
                                     receipt: dict, raw: bytes, cid: str) -> None:
    """S3Q.0.1 - the bindings that let a CLEAN CLONE check an EVALUATED_* claim.

    Everything above this point can be satisfied by a document that agrees with itself.
    These are the checks that reach OUTSIDE the receipt: to the tracked training receipt
    that sealed the weights, to the snapshot's own adapter digests, to the base model the
    control plane names, to Git, and - the one that matters most - to the production
    eligibility algorithm, which is asked what the receipt's own body-free evidence
    concludes rather than told what the receipt concluded.

    Loads no model, opens no socket, reads no held-out material, and does not require the
    gitignored evaluation tree to still exist. Runtime artefacts are gone by the time
    anyone audits this; that is the whole reason a receipt is written.
    """
    candidate = receipt.get("candidate", {})
    bound_training = receipt.get("training_receipt", {})
    ledger = receipt.get("ledger", {})

    # -- the terminal vocabulary is the PRODUCTION one, re-derived not restated --
    if str(_PACKAGE_ROOT) not in sys.path:
        sys.path.insert(0, str(_PACKAGE_ROOT))
    try:
        from training_gym.evaluation.config import EvaluationRunState
        from training_gym.evaluation.reports import ReportError, decision_from_evidence
    except Exception as exc:  # pragma: no cover - environment failure, reported not hidden
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the production evaluation module could not be imported "
                    f"({exc}); the receipt's verdict is therefore UNVERIFIED, which is a "
                    f"failure and not a pass")
        return
    derived = tuple(sorted(s.value for s in EvaluationRunState if s.is_terminal))
    if derived != tuple(sorted(TERMINAL_EVALUATION_EVENTS)):
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: this verifier's terminal vocabulary {TERMINAL_EVALUATION_EVENTS} "
                    f"is not the production one {derived}; a restated contract nobody "
                    f"re-derives is a second writable copy of it")
    if str(ledger.get("terminal_event", "")) not in derived:
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: terminal event {ledger.get('terminal_event')!r} is outside "
                    f"the production terminal vocabulary {list(derived)}")

    # -- the training receipt the SNAPSHOT names is the one the receipt bound ---
    pointer = str(entry.get("training_receipt") or "")
    if not pointer:
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: an evaluated candidate carries no training receipt; the "
                    f"weights that were measured have no sealed origin")
        return
    if bound_training.get("path") != pointer:
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the evaluation receipt binds training receipt "
                    f"{bound_training.get('path')!r} and the snapshot names {pointer!r}")
    if bound_training.get("candidate_id") != cid:
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the bound training receipt describes "
                    f"{bound_training.get('candidate_id')!r}")

    training_path = REPO_ROOT / pointer
    if training_path.is_symlink() or not training_path.is_file():
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the bound training receipt {pointer!r} is not a regular file")
        return
    training_bytes = training_path.read_bytes()
    if sha256_bytes(training_bytes) != bound_training.get("training_receipt_sha256"):
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the tracked training receipt does not hash to the digest "
                    f"the evaluation receipt bound; one of the two describes a different "
                    f"training run")
        return
    try:
        training = json.loads(training_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the bound training receipt is unreadable ({exc})")
        return

    # -- the adapter, agreed by THREE independent surfaces (S3Q.0.1 section 14) --
    # Snapshot, training receipt and evaluation receipt. None of them is permitted to
    # stand in for a missing other, and the runtime weights are NOT required: history
    # does not stop being true when a gitignored tree is deleted.
    trained_adapter = training.get("adapter", {})
    for label, snapshot_field, training_field, receipt_field in (
            ("adapter weights", "adapter_sha256", "sha256", "adapter_sha256"),
            ("adapter manifest", "adapter_manifest_hash", "manifest_hash",
             "adapter_manifest_hash")):
        values = {
            "the snapshot": str(entry.get(snapshot_field) or ""),
            "the training receipt": str(trained_adapter.get(training_field) or ""),
            "the evaluation receipt": str(candidate.get(receipt_field) or ""),
        }
        if len(set(values.values())) != 1:
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: the three surfaces disagree about the {label} - "
                        f"{ {k: v[:12] for k, v in values.items()} }")
    if str(trained_adapter.get("artifact_set_hash") or "") != \
            str(candidate.get("adapter_artifact_set_hash") or ""):
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the training receipt and the evaluation receipt disagree "
                    f"about the adapter artefact set")

    # -- the baseline the control plane names is the baseline that was measured -
    base = cp.snapshot.get("base_model", {})
    baseline = receipt.get("baseline", {})
    for label, mine, theirs in (("base model", baseline.get("model_id"),
                                 base.get("model_id")),
                                ("base revision", baseline.get("revision"),
                                 base.get("revision"))):
        if mine != theirs:
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: the receipt measured against {label} {mine!r} and the "
                        f"control plane names {theirs!r}")

    # -- the source the code came from is in this branch's history --------------
    # `.2` keeps one `source` block; `.3` splits it, and the EVALUATION source is the
    # one that has to be in this history -- the seal source is checked separately below.
    source = receipt.get("evaluation_source") or receipt.get("source", {})
    commit = str(source.get("evaluation_source_commit", ""))
    code, _ = _git("cat-file", "-e", f"{commit}^{{commit}}")
    if code != 0:
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: evaluation source commit {commit[:12]} is not an object in "
                    f"this repository; a receipt may not name code nobody can fetch")
    else:
        code, _ = _git("merge-base", "--is-ancestor", commit, "HEAD")
        if code != 0:
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: evaluation source commit {commit[:12]} is not an "
                        f"ancestor of HEAD; the evidence and the tree that carries it "
                        f"describe different histories")

    # -- the verdict, REDERIVED by the production algorithm (S3Q.0.1 FINDING I) -
    evidence = receipt.get("decision_evidence", {})
    try:
        decision = decision_from_evidence(
            gate_report=evidence.get("gate_report"),
            bootstrap=evidence.get("bootstrap"),
            empirical_status=evidence.get("empirical_status"),
            run_state=evidence.get("report_serialization_state"))
    except (ReportError, KeyError, TypeError, ValueError) as exc:
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the canonical eligibility could not be rederived from the "
                    f"receipt's own body-free evidence ({type(exc).__name__}: {exc}); "
                    f"'the receipt says so' is not a verified state")
        return
    expected = {
        "not_eligible": "EVALUATED_NOT_ELIGIBLE",
        "eligible_for_human_review": "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW",
        "needs_more_evidence": "EVALUATED_NEEDS_MORE_EVIDENCE",
        "quarantined": "EVALUATED_QUARANTINED",
    }.get(decision.eligibility.value, "")
    if candidate.get("status_claim") != expected:
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the receipt claims {candidate.get('status_claim')!r} and "
                    f"the canonical decision rederived from its own evidence supports "
                    f"{expected or 'no evaluated state'}")
    if decision.to_dict() != evidence.get("canonical_decision"):
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the receipt's recorded decision is not the one its evidence "
                    f"produces")
    if str(receipt.get("schema_version", "")) == EVAL_RECEIPT_V3_SCHEMA_VERSION:
        _check_seal_recovery_receipt(report, receipt=receipt, cid=cid)

    report.note(
        f"{cid}: evaluation receipt {receipt.get('receipt_version')} verified - adapter "
        f"{str(candidate.get('adapter_sha256'))[:12]} rooted in training receipt "
        f"{str(bound_training.get('training_receipt_sha256'))[:12]}, verdict "
        f"{decision.eligibility.value} REDERIVED from body-free evidence rather than "
        f"read from the claim")


def _check_seal_recovery_receipt(report: Report, *, receipt: dict, cid: str) -> None:
    """S3Q.0.2 - the three bindings only a `.3` receipt can be held to.

    All three reach OUTSIDE the receipt, which is the point: everything a document says
    about itself it can be edited to say.

      1. THE WITNESS IS REAL AND IS THE ONE BOUND. The tracked pre-repair witness is
         re-read, re-hashed and required to agree with the receipt about the evaluation
         source, the plan, the report, the three ledger event digests and the artefact
         identities. A receipt that binds a witness nobody can produce binds nothing.
      2. THE GIT TOPOLOGY IS THE ONE CLAIMED. The witness commit's FIRST PARENT must be
         the evaluation source commit. That single fact is what survives the repair: it
         was fixed before HEAD could move, and it cannot be re-created afterwards.
      3. THE VERDICT VOCABULARY IS PRODUCTION'S. Re-derived from `ComparisonVerdict`
         rather than read from the restated tuple, for the same reason the terminal
         vocabulary is: a literal nobody re-derives is a second writable copy.

    Loads no model, opens no socket, reads no held-out material and does not need the
    gitignored evaluation tree.
    """
    bound = receipt.get("measurement_witness", {})
    source = receipt.get("evaluation_source", {})
    seal = receipt.get("seal_implementation_source", {})
    results = receipt.get("results", {})

    # -- 3. the vocabulary, RE-DERIVED from production ------------------------
    if str(_PACKAGE_ROOT) not in sys.path:
        sys.path.insert(0, str(_PACKAGE_ROOT))
    try:
        from training_gym.evaluation.comparison import ComparisonVerdict
    except Exception as exc:  # pragma: no cover - environment failure, reported not hidden
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the production comparison module could not be imported "
                    f"({exc}); the receipt's verdict partition is therefore UNVERIFIED")
        return
    production = tuple(sorted(v.value for v in ComparisonVerdict))
    if production != tuple(sorted(COMPARISON_VERDICTS)):
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: this verifier's verdict vocabulary "
                    f"{sorted(COMPARISON_VERDICTS)} is not the production one "
                    f"{list(production)}; a restated contract nobody re-derives is a "
                    f"second writable copy of it")
    counts = results.get("verdict_counts", {})
    if tuple(sorted(counts)) != production:
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the receipt partitions its pairs over {sorted(counts)} and "
                    f"production classifies {list(production)}; a partition missing a "
                    f"verdict cannot be exhaustive")
    elif sum(counts.values()) != results.get("measured_pairs"):
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the verdict counts sum to {sum(counts.values())} against "
                    f"{results.get('measured_pairs')!r} measured pair(s)")
    if list(results.get("verdict_vocabulary", [])) != list(production):
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the receipt records verdict vocabulary "
                    f"{results.get('verdict_vocabulary')} and production defines "
                    f"{list(production)}")

    # -- 1. the witness, re-read from the tracked tree ------------------------
    pointer = str(bound.get("path") or "")
    path = REPO_ROOT / pointer
    if not pointer or path.is_symlink() or not path.is_file():
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the bound measurement witness {pointer!r} is not a regular "
                    f"tracked file; the evaluation source is then bound to nothing")
        return
    code, tracked = _git("ls-files", "--error-unmatch", "--", pointer)
    if code != 0 or not tracked:
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the measurement witness {pointer} is untracked; a bridge "
                    f"Git does not carry cannot cross a repair commit")
    raw = path.read_bytes()
    if sha256_bytes(raw) != bound.get("measurement_witness_sha256"):
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the tracked measurement witness does not hash to the digest "
                    f"the receipt bound; one of the two describes a different measurement")
        return
    try:
        witness = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the bound measurement witness is unreadable ({exc})")
        return
    for problem in validate_against_schema(measurement_witness_schema(), witness):
        report.fail("EVALUATION_RECEIPT", f"{cid}: measurement witness {problem}")
    body = {k: v for k, v in witness.items() if k != "witness_hash"}
    if witness.get("witness_hash") != sha256_bytes(canonical_bytes(body)):
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the measurement witness's own digest does not match its "
                    f"bytes")
    if witness.get("witness_hash") != bound.get("measurement_witness_hash"):
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the receipt records witness hash "
                    f"{str(bound.get('measurement_witness_hash'))[:12]} and the witness "
                    f"carries {str(witness.get('witness_hash'))[:12]}")

    # A witness that granted anything would be a receipt wearing another name.
    grants = witness.get("grants", {})
    if any(grants.get(flag) for flag in ("candidate_state", "promotion", "activation",
                                         "registry_mutation", "retry_or_rerun",
                                         "is_an_evaluation_receipt")):
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the bound measurement witness claims to grant something; a "
                    f"witness records facts and authorises nothing")
    if witness.get("evaluation_id") != receipt.get("evaluation_id") or \
            int(witness.get("evaluation_generation", -1)) != \
            int(receipt.get("evaluation_generation", -2)):
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the witness describes "
                    f"{witness.get('evaluation_id')!r} generation "
                    f"{witness.get('evaluation_generation')!r} and the receipt describes "
                    f"{receipt.get('evaluation_id')!r} generation "
                    f"{receipt.get('evaluation_generation')!r}")
    if witness.get("candidate_id") != cid:
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the bound witness describes {witness.get('candidate_id')!r}")

    witness_source = witness.get("evaluation_source", {})
    for field in ("evaluation_source_commit", "evaluation_source_tree_oid",
                  "evaluation_source_digest"):
        if source.get(field) != witness_source.get(field):
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: the receipt's {field} is "
                        f"{str(source.get(field))[:12]} and the pre-repair witness "
                        f"recorded {str(witness_source.get(field))[:12]}; the evaluation "
                        f"source must come from the witness, never from a repair-time "
                        f"HEAD")

    ledger, evidence = receipt.get("ledger", {}), receipt.get("evidence", {})
    w_ledger, w_evidence = witness.get("ledger", {}), witness.get("evidence", {})
    for section, mine, theirs, fields in (
            ("ledger", ledger, w_ledger,
             ("plan_hash", "plan_started_event_hash", "holdout_commit_event_hash",
              "terminal_event_hash", "terminal_event")),
            ("evidence", evidence, w_evidence,
             ("report_hash", "evaluation_manifest_hash",
              "evaluation_artifact_tree_hash", "comparison_manifest_hash",
              "metrics_summary_hash", "gate_report_hash", "bootstrap_report_hash"))):
        for field in fields:
            if mine.get(field) != theirs.get(field):
                report.fail("EVALUATION_RECEIPT",
                            f"{cid}: {section}.{field} is {str(mine.get(field))[:12]} in "
                            f"the receipt and {str(theirs.get(field))[:12]} in the "
                            f"witness")
    if receipt.get("plan", {}).get("plan_hash") != witness.get("plan", {}).get("plan_hash"):
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the receipt and the witness bind different plans")
    holdout, w_corpus = receipt.get("holdout", {}), witness.get("eval_corpus", {})
    for field in ("dataset_id", "dataset_version", "dataset_manifest_hash",
                  "task_pack_hash", "hidden_target_store_hash"):
        if holdout.get(field) != w_corpus.get(field):
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: holdout.{field} is {str(holdout.get(field))[:12]} in "
                        f"the receipt and {str(w_corpus.get(field))[:12]} in the "
                        f"witness; the corpus that was spent is not open to revision")
    if receipt.get("evidence", {}).get("pack_manifest_hash") != \
            w_corpus.get("pack_manifest_hash"):
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the receipt and the witness disagree about the pack manifest")
    if w_corpus.get("status_claim") != "USED_IMMUTABLE" or not w_corpus.get("spent_once"):
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the witness does not record the corpus as spent exactly "
                    f"once and immutable")
    w_results = witness.get("results", {})
    for field in ("task_count", "measured_pairs", "missing_pairs",
                  "total_model_result_count", "baseline_result_count",
                  "candidate_result_count", "paired_result_count"):
        if results.get(field) != w_results.get(field):
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: results.{field} is {results.get(field)!r} in the receipt "
                        f"and {w_results.get(field)!r} in the witness")
    # The witness predates the exhaustive-key rule and carries only what it observed.
    if {k: v for k, v in counts.items() if v} != w_results.get("verdict_counts"):
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the receipt's non-zero verdict counts are not the ones the "
                    f"witness recorded")
    if results.get("numeric_delta_counts") != w_results.get("numeric_delta_counts"):
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the receipt and the witness disagree about the numeric "
                    f"delta partition")
    if witness.get("outcome", {}).get("canonical_eligibility") != \
            receipt.get("outcome", {}).get("eligibility"):
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the witness rederived "
                    f"{witness.get('outcome', {}).get('canonical_eligibility')!r} and "
                    f"the receipt claims "
                    f"{receipt.get('outcome', {}).get('eligibility')!r}")

    # -- 2. the Git topology that survives the repair -------------------------
    witness_commit = str(bound.get("measurement_witness_commit", ""))
    code, parents = _git("rev-list", "--parents", "-n", "1", witness_commit)
    if code != 0 or not parents:
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: measurement witness commit {witness_commit[:12]} is not a "
                    f"commit in this repository")
    else:
        chain = parents.split()
        first_parent = chain[1] if len(chain) > 1 else ""
        if first_parent != str(source.get("evaluation_source_commit", "")):
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: the measurement witness commit's first parent is "
                        f"{first_parent[:12] or '(none)'} and the receipt names "
                        f"evaluation source "
                        f"{str(source.get('evaluation_source_commit'))[:12]}; the "
                        f"topology that fixes the evaluation source is the one thing a "
                        f"post-repair receipt cannot re-create")
        code, _ = _git("merge-base", "--is-ancestor", witness_commit, "HEAD")
        if code != 0:
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: the measurement witness commit is not an ancestor of "
                        f"HEAD")
        # The witness must be the file THAT commit carried, not one edited afterwards.
        code, blob = _git("rev-parse", f"{witness_commit}:{pointer}")
        code2, current = _git("hash-object", "--", str(path))
        if code == 0 and code2 == 0 and blob != current:
            report.fail("EVALUATION_RECEIPT",
                        f"{cid}: the tracked measurement witness is not the blob its own "
                        f"commit recorded; a bridge edited after it was laid is not one")

    # -- the two sources are separate, and the seal source is real ------------
    seal_commit = str(seal.get("seal_implementation_source_commit", ""))
    code, _ = _git("cat-file", "-e", f"{seal_commit}^{{commit}}")
    if code != 0:
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: seal implementation source {seal_commit[:12]} is not a "
                    f"commit in this repository")
    elif _git("merge-base", "--is-ancestor", seal_commit, "HEAD")[0] != 0:
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: seal implementation source {seal_commit[:12]} is not an "
                    f"ancestor of HEAD")
    differs = seal_commit != str(source.get("evaluation_source_commit", ""))
    if bool(seal.get("differs_from_evaluation_source")) != differs:
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the receipt says the seal and evaluation sources "
                    f"{'differ' if seal.get('differs_from_evaluation_source') else 'match'} "
                    f"and the commits say otherwise")
    if differs and _git("merge-base", "--is-ancestor",
                        str(source.get("evaluation_source_commit", "")),
                        seal_commit)[0] != 0:
        report.fail("EVALUATION_RECEIPT",
                    f"{cid}: the seal implementation source does not descend from the "
                    f"evaluation source; a repair that is not built on the evaluated "
                    f"code is not a repair of this measurement")
    report.note(
        f"{cid}: seal recovery verified - measured at "
        f"{str(source.get('evaluation_source_commit'))[:12]}, sealed at "
        f"{seal_commit[:12]}, bridged by witness "
        f"{str(bound.get('measurement_witness_sha256'))[:12]} whose first parent IS the "
        f"evaluation source; {sum(counts.values()) if counts else 0} pair(s) partitioned "
        f"over {len(production)} production verdict(s)")


def transition_problems(before: str, after: str, table: dict, label: str) -> list[str]:
    """Return why ``before -> after`` is refused. Empty means the table permits it."""
    if before not in table:
        return [f"{label} state {before!r} is not recognised"]
    allowed = table[before]
    if after not in allowed:
        return [f"{label} transition {before} -> {after} is not in the allowed table "
                f"(permitted from {before}: {sorted(allowed) or 'nothing — terminal'})"]
    return []


def _parent_snapshot(cp: ControlPlane) -> "dict | None":
    """The previous generation, in its V2 SEMANTIC shape whatever its container.

    S4B. The rehydration here is not a nicety: the transition table reads
    ``parent["candidates"]``, and a V3 generation stores candidates as a content-addressed
    RECORD, so a raw read of a V3 parent yields a document with no ``candidates`` key at
    all. Every ordinal then looks ABSENT from the parent, which reads as four sealed
    candidates entering the control plane for the first time at ``EVALUATED_*`` -- a
    failure with an actively misleading message, and, for a check whose entire job is to
    refuse illegal transitions, the first generation with a V3 parent would have had its
    transition table quietly reduced to the fresh-ordinal rule.

    It failed CLOSED rather than passing, which is why it was found rather than shipped.
    The fix is to read the parent the way :func:`load` reads the current generation.
    """
    generation = cp.snapshot.get("state_generation", 1)
    if generation <= 1:
        return None
    directory = REPO_ROOT / SNAPSHOT_DIR
    for path in sorted(directory.iterdir()):
        if path.suffix == ".json" and path.name.startswith(f"{generation - 1:04d}-"):
            payload, _ = _load_json(path, path.name)
            if payload.get("schema_version") != CONTROL_PLANE_V3_SCHEMA_VERSION:
                return payload
            rehydrated, problems = rehydrate_v3(
                payload, load_record_store(REPO_ROOT / RECORD_DIR))
            # A parent that cannot be rehydrated is NOT treated as "no parent": that
            # would skip the transition table entirely, which is the one outcome a
            # broken record store must never buy. `None` here means the caller reports
            # nothing, so the missing-parent branch is deliberately not reused; the
            # RECORD_STORE check fails independently on the same store.
            return None if problems else rehydrated
    return None


def check_policy_identities(cp: ControlPlane, report: Report) -> None:
    """V21 — re-derive the policy digests from the production classes, do not read them."""
    declared = cp.snapshot.get("policy_identities", {})
    if str(_PACKAGE_ROOT) not in sys.path:
        sys.path.insert(0, str(_PACKAGE_ROOT))
    try:
        from training_gym.evaluation.gates import GatePolicy
        from training_gym.evaluation.generation import (
            DevicePolicy, PrecisionPolicy, eligibility_generation_policy)
        from training_gym.evaluation.policy import MetricPolicy
    except Exception as exc:  # pragma: no cover - environment failure, reported not hidden
        report.fail("POLICY_IDENTITIES",
                    f"the production policy classes could not be imported ({exc}); the "
                    f"declared digests are therefore UNVERIFIED, which is a failure and "
                    f"not a pass")
        return

    configured = eligibility_generation_policy(
        seed=11, timeout_s=300, device_policy=DevicePolicy.CPU,
        precision_policy=PrecisionPolicy.FP32)
    derived = {
        "gate_policy_hash": GatePolicy().policy_hash(),
        "metric_policy_hash": MetricPolicy().policy_hash(),
        "generation_policy_hash": configured.policy_hash(),
        "generation_policy_constructor_default_hash":
            eligibility_generation_policy().policy_hash(),
    }
    for key, value in derived.items():
        if declared.get(key) != value:
            report.fail("POLICY_IDENTITIES",
                        f"{key}: the snapshot says {declared.get(key)}, the production "
                        f"class re-derives {value}")
    if declared.get("max_new_tokens") != configured.max_new_tokens:
        report.fail("POLICY_IDENTITIES",
                    f"max_new_tokens: snapshot {declared.get('max_new_tokens')}, policy "
                    f"{configured.max_new_tokens}")
    if declared.get("reasoning_policy") != configured.reasoning_policy.value.upper():
        report.fail("POLICY_IDENTITIES",
                    f"reasoning_policy: snapshot {declared.get('reasoning_policy')}, "
                    f"policy {configured.reasoning_policy.value.upper()}")
    if derived["generation_policy_hash"] == \
            derived["generation_policy_constructor_default_hash"]:
        report.fail("POLICY_IDENTITIES",
                    "the configured and constructor-default generation policies hash "
                    "alike; the S3N reconciliation says they are different objects")

    # V20 - defect statuses against the current instrument, measured not quoted.
    defects = {d.get("id"): d for d in cp.snapshot.get("defects", [])}
    for did, status in FROZEN_DEFECT_STATUSES.items():
        entry = defects.get(did)
        if entry is None:
            report.fail("POLICY_IDENTITIES", f"{did} is absent from the defect ledger")
        elif entry.get("status") != status:
            report.fail("POLICY_IDENTITIES",
                        f"{did}: status {entry.get('status')!r}, the milestone authority "
                        f"says {status!r}")
    if defects.get("D38", {}).get("is_gate") is not False:
        report.fail("POLICY_IDENTITIES",
                    "D38 is recorded as a gate; S3M.2 designed none and none may be "
                    "added without a separate operator decision")
    try:
        gates_source = (_PACKAGE_ROOT / "training_gym" / "evaluation" /
                        "gates.py").read_text(encoding="utf-8")
    except OSError as exc:
        report.fail("POLICY_IDENTITIES", f"gates.py could not be read ({exc})")
    else:
        for needle in ("output_budget_exhaust", "finish_reason"):
            if needle in gates_source:
                report.fail("POLICY_IDENTITIES",
                            f"gates.py references {needle!r}; a D38 gate has appeared")


def check_authority_separation(cp: ControlPlane, report: Report) -> None:
    """V25 — nothing in the control plane may read as a grant, and none of it is one."""
    observation = cp.snapshot.get("authority_observation", {})
    for key in ("train", "eval", "promotion"):
        if observation.get(key) not in AUTHORITY_OBSERVATIONS:
            report.fail("AUTHORITY_SEPARATION",
                        f"authority_observation.{key} is {observation.get(key)!r}, which "
                        f"is not a recognised observation")
    if observation.get("control_plane_can_grant_authority") is not False:
        report.fail("AUTHORITY_SEPARATION",
                    "the snapshot does not state that the control plane cannot grant "
                    "authority")

    for label, payload in (("current.json", cp.current), ("snapshot", cp.snapshot),
                           ("migration manifest", cp.migration)):
        for problem in _authority_shaped(payload, label):
            report.fail("AUTHORITY_SEPARATION", problem)

    # The observation is MEASURED: no tracked file may carry a spendable token literal.
    code, out = _git("grep", "-I", "-n", "-E", TOKEN_LITERAL_PATTERN, "--", ".")
    if code == 0 and out:
        report.fail("AUTHORITY_SEPARATION",
                    f"a spendable plan-token literal appears in tracked files: "
                    f"{out.splitlines()[0][:160]}")
    elif code not in (0, 1):
        report.fail("AUTHORITY_SEPARATION",
                    "the token-literal scan could not run, so the authority observation "
                    "is UNKNOWN rather than none")

    # Authority-shaped PROSE. Flagged, never honoured. A sentence saying a run is
    # authorised is a sentence; the capability lives in a single-use plan token this
    # control plane cannot mint, and the note exists so a human notices someone tried.
    for rel in SCANNED_SURFACES:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            if AUTHORITY_CLAIM_RE.search(line):
                report.note(
                    f"AMBIGUOUS AUTHORITY CLAIM in {rel}:{line_number} — prose that "
                    f"reads as a grant. It grants nothing: no capability is created by "
                    f"any document. Review why it is there.")


def _authority_shaped(payload: object, label: str, path: str = "$") -> list[str]:
    problems: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            if lowered in FORBIDDEN_AUTHORITY_KEYS:
                problems.append(
                    f"{label}: key {path}.{key} reads as an authority grant; the control "
                    f"plane may only carry an 'authority_observation'")
            problems.extend(_authority_shaped(value, label, f"{path}.{key}"))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            problems.extend(_authority_shaped(item, label, f"{path}[{index}]"))
    elif isinstance(payload, str) and TOKEN_LITERAL_RE.search(payload):
        problems.append(f"{label}: {path} holds something shaped like a plan token")
    return problems


def check_operator_ruling(cp: ControlPlane, report: Report) -> None:
    """S3U, S4B — a candidate whose design rests on an operator ruling shows that ruling.

    The failure this prevents is a supersession nobody can audit: a candidate appears at
    an axis a standing entry forbade, the snapshot's prose says a human allowed it, and
    no tracked artefact records who decided what. Prose cannot grant authority, so the
    ruling is not authority here either — it is EVIDENCE that a decision was made, and
    it is required to exist, to be tracked, and to be checkable.

    The one thing it must NOT contain is the authorisation phrase itself. A control plane
    holding a replayable string would be holding a capability, so the record carries a
    digest and says so; a record that stored the literal is a FAILURE, not a convenience.

    S4B made this a LOOP over :data:`OPERATOR_RULINGS`. S3U's version named candidate 004
    and its one ruling directly, which was correct while there was one; a second candidate
    resting on a second ruling turns that shape into a check that silently covers the
    first and ignores the rest. Each ruling is re-derived independently against the
    production generator, so two rulings cannot vouch for each other.
    """
    if str(_PACKAGE_ROOT) not in sys.path:
        sys.path.insert(0, str(_PACKAGE_ROOT))
    generator = None
    recorded = {c.get("candidate_id") for c in cp.snapshot.get("candidates", [])}

    for cid, (key, rel) in sorted(OPERATOR_RULINGS.items()):
        if cid not in recorded:
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            report.fail("AUTHORITY_SEPARATION",
                        f"{cid} is recorded, but the operator ruling it rests on "
                        f"({rel}) is not a file in this tree")
            continue
        code, out = _git("ls-files", "--error-unmatch", "--", rel)
        if code != 0 or not out:
            report.fail("AUTHORITY_SEPARATION",
                        f"{rel} is untracked; a ruling with no history has no second "
                        f"witness")
        payload, raw = _load_json(path, rel)
        if raw != canonical_bytes(payload):
            report.fail("AUTHORITY_SEPARATION",
                        f"{rel} is not in the canonical serialization every "
                        f"control-plane digest is taken over")
        if payload.get("ruling_phrase_recorded") is not False:
            report.fail("AUTHORITY_SEPARATION",
                        f"{rel} does not state that the authorisation phrase is "
                        f"withheld")
        if not SHA256_RE.match(str(payload.get("ruling_phrase_sha256", ""))):
            report.fail("AUTHORITY_SEPARATION",
                        f"{rel} carries no digest of the phrase it records, so which "
                        f"decision was given is unauditable")
        if payload.get("scope") != "DESIGN_ONLY":
            report.fail("AUTHORITY_SEPARATION",
                        f"{rel} claims scope {payload.get('scope')!r}; the ruling "
                        f"recorded at that generation authorised a DESIGN and nothing "
                        f"else")

        # The ruling and the repository must name the SAME experiment. Re-derived from
        # the production generator, so a ruling that drifted from what was built is a
        # failure rather than a second opinion.
        if generator is None:
            try:
                from scripts import build_quality_training_config as generator
            except Exception as exc:
                report.fail("AUTHORITY_SEPARATION",
                            f"the production candidate generator could not be imported "
                            f"({exc}); the operator rulings are therefore UNVERIFIED")
                return
        reference_key, declared = generator.CANDIDATE_SINGLE_AXIS[key]
        expected = {
            "subject_candidate": generator.CANDIDATES[key]["run_id"],
            "reference_candidate": generator.CANDIDATES[reference_key]["run_id"],
            "primary_axis": sorted(declared)[0],
            "reference_value": generator.format_learning_rate(
                generator.OPTIONS[generator.CANDIDATE_OPTION[reference_key]][
                    "learning_rate"]),
            "ruled_value": generator.format_learning_rate(
                generator.OPTIONS[generator.CANDIDATE_OPTION[key]]["learning_rate"]),
        }
        for field, value in expected.items():
            if payload.get(field) != value:
                report.fail("AUTHORITY_SEPARATION",
                            f"{rel} records {field}={payload.get(field)!r}; the "
                            f"repository builds {value!r}")
        superseded = payload.get("supersedes", {})
        if superseded.get("historical_entry_erased") is not False:
            report.fail("AUTHORITY_SEPARATION",
                        f"{rel} does not state that the historical entry it supersedes "
                        f"remains factual at the generation that made it")
        report.note(f"{cid}: operator ruling {payload.get('ruling_id')} verified "
                    f"body-free - {expected['primary_axis']} "
                    f"{expected['reference_value']} -> {expected['ruled_value']}, scope "
                    f"DESIGN_ONLY, phrase withheld and carried as a digest")

    # Two rulings may not share a digest, an id or a subject. A copied ruling record is
    # the cheapest way to make a second candidate look independently decided.
    seen: dict[str, list[str]] = {}
    for cid, (_key, rel) in sorted(OPERATOR_RULINGS.items()):
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        payload, _raw = _load_json(path, rel)
        for field in ("ruling_id", "ruling_phrase_sha256", "subject_candidate"):
            seen.setdefault(f"{field}={payload.get(field)}", []).append(rel)
    for marker, owners in sorted(seen.items()):
        if len(owners) > 1:
            report.fail("AUTHORITY_SEPARATION",
                        f"operator rulings {owners} share {marker}; a ruling copied "
                        f"from another is not a second human decision")


def check_holdout_firewall(cp: ControlPlane, report: Report) -> None:
    """V23, V24 — no task body, no body source pointer, no secret, no private path.

    The firewall is enforced WITHOUT opening a single held-out body. Three structural
    rules do the work: a control-plane document may not name an individual holdout task,
    may not cite a body-bearing source as evidence, and may not carry a long free-text
    string or a body-shaped key for one to hide in.
    """
    for rel in SCANNED_SURFACES:
        path = REPO_ROOT / rel
        if not path.is_file():
            report.fail("HOLDOUT_FIREWALL", f"{rel} is missing and cannot be scanned")
            continue
        text = path.read_text(encoding="utf-8")

        for held_out, task_ids in HELD_OUT_TASK_IDS.items():
            named = sorted({tid for tid in task_ids if tid in text})
            if named:
                report.fail("HOLDOUT_FIREWALL",
                            f"{rel} names individual eval-{held_out} task(s) {named[:4]}; "
                            f"a routine bootstrap surface has no reason to carry per-task "
                            f"material")
        for symbol in FORBIDDEN_BODY_SYMBOLS:
            if symbol in text:
                report.fail("HOLDOUT_FIREWALL",
                            f"{rel} references {symbol!r}, the "
                            f"eval-{body_symbol_version(symbol)} body source")
        for category in _scan_leaks(text):
            report.fail("HOLDOUT_FIREWALL", f"{rel}: leak scanner reports {category!r}")
        for match in PRIVATE_PATH_RE.findall(text):
            report.fail("HOLDOUT_FIREWALL", f"{rel} carries a private host path {match!r}")
        try:
            text.encode("ascii")
        except UnicodeEncodeError:
            if rel.endswith(".json"):
                report.fail("HOLDOUT_FIREWALL",
                            f"{rel} is not ASCII; control-plane JSON must be, so its "
                            f"canonical bytes cannot depend on an encoding choice")

    for label, payload in (("current.json", cp.current), ("snapshot", cp.snapshot),
                           ("migration manifest", cp.migration)):
        for problem in _body_shaped(payload, label):
            report.fail("HOLDOUT_FIREWALL", problem)

    # V23 - no evidence pointer may lead into a body-bearing file.
    for label, payload in (("snapshot", cp.snapshot),
                           ("migration manifest", cp.migration)):
        for pointer in _string_values(payload):
            for forbidden in FORBIDDEN_BODY_SOURCES:
                if pointer.endswith(forbidden) or pointer == forbidden:
                    report.fail("HOLDOUT_FIREWALL",
                                f"{label} cites {pointer!r}, which holds eval-v4 task "
                                f"bodies; cite the body-free freeze document instead")

    # The archive is history, not a bootstrap surface, and must stay out of the
    # bootstrap set so a routine session never pays for it.
    if ARCHIVE_PATH in BOOTSTRAP_SURFACES:
        report.fail("HOLDOUT_FIREWALL", "the historical archive is in the bootstrap set")


#: Holdouts RETIRED FROM ELIGIBILITY USE, mapped to the generation the ruling took effect.
#: A retired holdout is a different thing from a spent one, and the distinction is the
#: whole point: eval-v5 was frozen, NEVER model-spent, and retired anyway because the
#: preregistered orchestrator body-blindness precondition failed BEFORE any authorisation
#: existed (D44). Relaxing a preregistered gate after it fails is post-hoc protocol
#: weakening, so the conservative remedy is retirement rather than reuse.
RETIRED_ELIGIBILITY_HOLDOUTS = {("m62-defensive-eval", "v5"): 12}

#: Machine-verifiable markers the retirement is carried by. The dataset STATUS vocabulary
#: cannot express "frozen, never model-spent, but retired from eligibility" and was not
#: extended to fake it, so the rule lives on the invariant surface and is enforced here.
RETIREMENT_MARKER = "ELIGIBILITY_USE: RETIRED"
FRESH_HOLDOUT_MARKER = "FRESH_V6_REQUIRED"


def _names_holdout(text: str, dataset_id: str, version: str) -> bool:
    """True when *text* designates that specific held-out corpus version."""
    return f"eval-{version}" in text or f"{dataset_id} {version}" in text


def check_holdout_retirement(cp: ControlPlane, report: Report) -> None:
    """A retired holdout may never come back as eligibility evidence.

    Reported under HOLDOUT_FIREWALL because it IS the firewall, extended in time: the
    body-blindness gate failed once for eval-v5, and the remedy only holds if a later
    session cannot quietly spend it anyway. Six independent conditions carry the rule, so
    no single edit relaxes it.

    Deliberately PROSPECTIVE. Generations 7 to 11 truthfully called eval-v5
    ``FROZEN_UNUSED`` before the exposure was known and stay byte-exact; this check does
    nothing to a snapshot older than the generation the ruling took effect.
    """
    generation = cp.snapshot.get("state_generation", 0)
    for (dataset_id, version), effective_from in RETIRED_ELIGIBILITY_HOLDOUTS.items():
        if generation < effective_from:
            continue
        label = f"{dataset_id} {version}"

        # 1. The lifecycle fact. No model ever saw it, so the dataset record must keep
        #    saying so. Marking it USED_IMMUTABLE to "represent" the incident would be a
        #    measurement claim nothing supports.
        entries = [d for d in cp.snapshot.get("datasets", [])
                   if d.get("dataset_id") == dataset_id and d.get("version") == version]
        if len(entries) != 1:
            report.fail("HOLDOUT_FIREWALL",
                        f"{label} is retired but appears {len(entries)} times in datasets")
            continue
        entry = entries[0]
        if entry.get("status") != "FROZEN_UNUSED" or entry.get("spent_by") is not None:
            report.fail("HOLDOUT_FIREWALL",
                        f"{label} is recorded status={entry.get('status')!r} "
                        f"spent_by={entry.get('spent_by')!r}. It was NEVER model-spent: "
                        f"0 weight loads, 0 generations, 0 holdout spend events and no "
                        f"receipt. Retirement is an ELIGIBILITY ruling and may not be "
                        f"written into the dataset lifecycle as a spend that never "
                        f"happened")

        # 2. The invariant surface must still carry the ruling, and the replacement
        #    requirement must still be stated.
        invariants = " ".join(cp.snapshot.get("frozen_invariants", []))
        if RETIREMENT_MARKER not in invariants:
            report.fail("HOLDOUT_FIREWALL",
                        f"no frozen invariant carries {RETIREMENT_MARKER!r}; the {label} "
                        f"eligibility retirement was dropped rather than superseded")
        if FRESH_HOLDOUT_MARKER not in invariants:
            report.fail("HOLDOUT_FIREWALL",
                        f"no frozen invariant carries {FRESH_HOLDOUT_MARKER!r}; a retired "
                        f"holdout leaves the replacement requirement, and dropping it "
                        f"would leave candidate 004 with no path to eligibility")

        # 3. No candidate may claim it as the corpus it was measured against.
        for candidate in cp.snapshot.get("candidates", []):
            if candidate.get("evaluation_corpus") == label:
                report.fail("HOLDOUT_FIREWALL",
                            f"candidate {candidate.get('candidate_id')} names retired "
                            f"{label} as its evaluation_corpus")

        # 4-6. The next milestone may not point a future session back at it.
        nxt = cp.snapshot.get("next_milestone", {})
        holdout_text = nxt.get("evaluation_holdout", "")
        if _names_holdout(holdout_text, dataset_id, version) and (
                "RETIRED" not in holdout_text):
            report.fail("HOLDOUT_FIREWALL",
                        f"next_milestone.evaluation_holdout names {label} without saying "
                        f"it is RETIRED, which reads as a designation to spend it")
        for required in nxt.get("authority_required", []):
            if _names_holdout(required, dataset_id, version):
                report.fail("HOLDOUT_FIREWALL",
                            f"next_milestone.authority_required requests authority naming "
                            f"retired {label}: {required[:80]!r}")
        if not any(_names_holdout(rule, dataset_id, version)
                   for rule in nxt.get("ruled_out", [])):
            report.fail("HOLDOUT_FIREWALL",
                        f"next_milestone.ruled_out does not restate the {label} "
                        f"prohibition where the next session actually reads it")


def _body_shaped(payload: object, label: str, path: str = "$") -> list[str]:
    problems: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in FORBIDDEN_BODY_KEYS:
                problems.append(f"{label}: key {path}.{key} could hold task material")
            problems.extend(_body_shaped(value, label, f"{path}.{key}"))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            problems.extend(_body_shaped(item, label, f"{path}[{index}]"))
    elif isinstance(payload, str) and len(payload) > MAX_JSON_STRING_CHARS:
        problems.append(f"{label}: {path} is {len(payload)} characters, over the "
                        f"{MAX_JSON_STRING_CHARS} limit that stops a body arriving in "
                        f"instalments")
    return problems


def _string_values(payload: object):
    if isinstance(payload, dict):
        for value in payload.values():
            yield from _string_values(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _string_values(item)
    elif isinstance(payload, str):
        yield payload


def _scan_leaks(text: str) -> list[str]:
    """Use the repository's own scanner when it is importable; never invent a weaker one."""
    if str(_PACKAGE_ROOT) not in sys.path:
        sys.path.insert(0, str(_PACKAGE_ROOT))
    try:
        from core.redaction_policy import scan_for_leaks
    except Exception:  # pragma: no cover - the private-path regex below still applies
        return []
    # `reasoning` — a literal `<think` marker — is hygiene rather than a security leak
    # under operator ruling H4, and it is the one category these documents legitimately
    # carry when they describe the D24 defect. Every other category, `home_path`
    # included, is a hard failure here; `PRIVATE_PATH_RE` is a second, independent
    # opinion on the same question rather than a substitute for this one.
    return [c for c in scan_for_leaks(text) if c != "reasoning"]


def check_record_store(cp: ControlPlane, report: Report) -> None:
    """V69 M63 — the V3 record store is complete, tracked and self-verifying.

    Under V2 this is a no-op: there is no store, and its absence is correct.
    """
    if not cp.is_v3:
        if (REPO_ROOT / RECORD_DIR).is_dir():
            report.note(f"{RECORD_DIR} exists while the newest generation is V2; "
                        f"records are inert until a V3 generation references them")
        return

    if cp.rehydration_problems:
        # Already reported by load(); restated here so the category is right.
        report.fail("RECORD_STORE",
                    f"the generation did not rehydrate: "
                    f"{cp.rehydration_problems[0]}")
        return

    directory = REPO_ROOT / RECORD_DIR
    if directory.is_symlink():
        report.fail("RECORD_STORE", f"{RECORD_DIR} is a symlinked directory")
        return
    if not directory.is_dir():
        report.fail("RECORD_STORE", f"{RECORD_DIR} is missing but the generation "
                                    f"references records")
        return

    referenced = set(cp.snapshot_stored.get("records", {}).values())
    rel_paths = []
    for digest in sorted(referenced):
        rel = f"{RECORD_DIR}/{digest}.json"
        path = REPO_ROOT / rel
        rel_paths.append(rel)
        if path.is_symlink():
            report.fail("RECORD_STORE", f"{rel} is a symlink")
            continue
        if not path.is_file():
            report.fail("RECORD_STORE", f"{rel} is referenced but is not a file")
            continue
        if os.access(path, os.X_OK):
            report.fail("RECORD_STORE", f"{rel} carries an executable bit")
        raw = path.read_bytes()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            report.fail("RECORD_STORE", f"{rel} is unreadable: {exc}")
            continue
        # The file must BE its own address, and it must be in the canonical
        # serialization — a record stored with different whitespace would hash
        # differently and silently stop resolving.
        if raw != canonical_bytes(payload):
            report.fail("RECORD_STORE",
                        f"{rel} is not in the canonical serialization")
            continue
        measured = sha256_bytes(canonical_bytes(payload))
        if measured != digest:
            report.fail("RECORD_STORE",
                        f"{rel} hashes to {measured}; it is referenced as {digest}")

    code, tracked_out = _git("ls-files", "-z", "--", *rel_paths) if rel_paths else (0, "")
    tracked = set(tracked_out.split("\0")) if code == 0 else set()
    if code == 0:
        for rel in rel_paths:
            if rel not in tracked:
                report.fail("RECORD_STORE",
                            f"{rel} is not tracked by Git; an untracked record has "
                            f"no history and no second witness")
    else:
        report.fail("RECORD_STORE", "git ls-files failed; record tracking "
                                    "cannot be verified")

    # A round-trip proof, run live rather than trusted from migration time.
    restored, problems = rehydrate_v3(cp.snapshot_stored, cp.records)
    if problems:
        report.fail("RECORD_STORE", f"live rehydration failed: {problems[0]}")
    elif canonical_bytes(restored) != canonical_bytes(cp.snapshot):
        report.fail("RECORD_STORE",
                    "live rehydration does not reproduce the loaded snapshot")
    else:
        report.note(f"V3 generation: {len(referenced)} records resolved, "
                    f"snapshot {len(cp.snapshot_bytes)} bytes on disk vs "
                    f"{len(canonical_bytes(cp.snapshot))} rehydrated")


def check_budgets(cp: ControlPlane, report: Report) -> None:
    """The size guards that stop the control plane becoming monolithic again."""
    progress = REPO_ROOT / PROGRESS_PATH
    text = progress.read_text(encoding="utf-8")
    lines = text.count("\n")
    size = progress.stat().st_size
    if lines > PROGRESS_MAX_LINES:
        report.fail("CONTROL_PLANE_BUDGET",
                    f"PROGRESS.md is {lines} lines, over the reviewed budget of "
                    f"{PROGRESS_MAX_LINES}. Deep detail belongs in a milestone document; "
                    f"raising this budget is an explicit control-plane migration")
    if size > PROGRESS_MAX_BYTES:
        report.fail("CONTROL_PLANE_BUDGET",
                    f"PROGRESS.md is {size} bytes, over the budget of "
                    f"{PROGRESS_MAX_BYTES}")
    if len(cp.snapshot_bytes) > SNAPSHOT_MAX_BYTES:
        report.fail("CONTROL_PLANE_BUDGET",
                    f"the snapshot is {len(cp.snapshot_bytes)} bytes, over "
                    f"{SNAPSHOT_MAX_BYTES}; state carries pointers, not reports")
    if len(cp.current_bytes) > CURRENT_MAX_BYTES:
        report.fail("CONTROL_PLANE_BUDGET",
                    f"current.json is {len(cp.current_bytes)} bytes, over "
                    f"{CURRENT_MAX_BYTES}; it is a pointer, not a second state")
    index = REPO_ROOT / HISTORY_INDEX_PATH
    if index.is_file() and index.stat().st_size > HISTORY_INDEX_MAX_BYTES:
        report.fail("CONTROL_PLANE_BUDGET",
                    f"{HISTORY_INDEX_PATH} is {index.stat().st_size} bytes, over "
                    f"{HISTORY_INDEX_MAX_BYTES}; an index routes, it does not repeat")

    bootstrap = sum((REPO_ROOT / rel).stat().st_size
                    for rel in BOOTSTRAP_SURFACES if (REPO_ROOT / rel).is_file())
    bootstrap += len(cp.snapshot_bytes)
    report.note(f"normal bootstrap surface: {len(BOOTSTRAP_SURFACES) + 1} files, "
                f"{bootstrap} bytes")


#: Substrings the CURRENT prospective rule must still bar, whatever else it says.
#:
#: A ``ruled_out`` list is rewritten at a generation that supersedes part of it, and the
#: failure mode is not that the rewrite is wrong -- it is that a rewrite aimed at one
#: clause quietly drops four others nobody was thinking about. These are checked as
#: substrings of the joined list rather than as exact entries, so the wording stays free
#: and the coverage does not.
REQUIRED_RULED_OUT_SUBJECTS = (
    "epoch", "rank", "alpha", "dropout", "ATTENTION_ONLY", "train-v3", "eval-v4",
    "eval-v5", "max_new_tokens", "grader", "threshold", "refusal detector",
    "candidate 003", "promotion",
)


def _check_primary_axis(cp: ControlPlane, nxt: dict, report: Report) -> None:
    """V26 — the recorded axis is the one the production generator actually configures.

    The field describes the experiment that is currently OPEN, so what it must say
    depends on the state and not on a date: while a candidate with an UNMEASURED axis
    exists, the axis is that candidate's and is re-derived from the generator; with no
    such candidate the field records the last measured axis, which is candidate 003's
    preregistered render-policy transition.

    S3V widened "unmeasured" from ``DESIGNED_UNTRAINED`` alone to include
    ``TRAINED_UNEVALUATED``, for the same reason S3P widened `check_candidate_design`:
    training a candidate does not answer its question. Candidate 004's experiment is at
    its most open the moment weights exist and no measurement does, and the narrower
    reading would have demanded the snapshot advertise candidate 003's already-measured
    render-policy axis as the live one -- retiring an open experiment from the control
    plane at exactly the moment it became real.

    Re-derived, never matched against a literal in this file. A verifier constant that
    agreed with a snapshot while the generator built something else would be the exact
    circular pass this module is built to refuse.
    """
    recorded = str(nxt.get("primary_axis", ""))
    designed = [c for c in cp.snapshot.get("candidates", [])
                if c.get("status") in ("DESIGNED_UNTRAINED", "TRAINED_UNEVALUATED")]
    if not designed:
        if "MODEL_DEFAULT" not in recorded or "DISABLED" not in recorded:
            report.fail("CANDIDATE_STATE",
                        f"the preregistered primary axis is not recorded: {recorded!r}")
        return

    if str(_PACKAGE_ROOT) not in sys.path:
        sys.path.insert(0, str(_PACKAGE_ROOT))
    try:
        from scripts import build_quality_training_config as generator
    except Exception as exc:
        report.fail("CANDIDATE_STATE",
                    f"the production candidate generator could not be imported ({exc}); "
                    f"the recorded primary axis is therefore UNVERIFIED")
        return

    for entry in designed:
        cid = entry.get("candidate_id")
        key = next((k for k, spec in generator.CANDIDATES.items()
                    if spec.get("run_id") == cid), "")
        relation = generator.CANDIDATE_SINGLE_AXIS.get(key) if key else None
        if relation is None:
            continue  # check_candidate_design already failed this candidate
        reference_key, declared = relation
        for dial in sorted(declared):
            if dial not in recorded:
                report.fail("CANDIDATE_STATE",
                            f"{cid}: its single axis is {dial!r}, which the recorded "
                            f"primary_axis {recorded!r} does not name")
                continue
            before = generator.OPTIONS[generator.CANDIDATE_OPTION[reference_key]][dial]
            after = generator.OPTIONS[generator.CANDIDATE_OPTION[key]][dial]
            render = (generator.format_learning_rate if dial == "learning_rate"
                      else str)
            for value, side in ((before, f"candidate {reference_key}'s"),
                                (after, f"{cid}'s")):
                if render(value) not in recorded:
                    report.fail("CANDIDATE_STATE",
                                f"{cid}: the recorded primary_axis does not carry "
                                f"{side} {dial} ({render(value)}); an axis recorded "
                                f"without its two ends is not a measurable claim")


def _check_ruled_out(nxt: dict, report: Report) -> None:
    """The current prospective rule still bars everything it is not superseding.

    A supersession is narrow by construction or it is not a supersession. This checks
    the two halves of that: the subjects that must remain barred are still named, and a
    learning-rate permission -- the one clause generation 9 superseded -- is scoped to
    the candidate it was ruled for rather than opened for every future candidate.
    """
    entries = [str(x) for x in nxt.get("ruled_out", [])]
    joined = " | ".join(entries)
    missing = [s for s in REQUIRED_RULED_OUT_SUBJECTS if s not in joined]
    if missing:
        report.fail("CANDIDATE_STATE",
                    f"the current ruled_out list no longer bars {missing}; a rewrite "
                    f"that supersedes one clause may not drop the others")
    permissions = [e for e in entries
                   if "learning" in e.lower() and "candidate 004" not in e]
    for entry in permissions:
        if any(word in entry.lower() for word in ("allow", "permit", "may ")):
            report.fail("CANDIDATE_STATE",
                        f"ruled_out entry {entry!r} reads as an unscoped learning-rate "
                        f"permission; the supersession is candidate-004-specific")


def check_next(cp: ControlPlane, report: Report) -> None:
    """V26, V27 — the NEXT contract and the test baseline are the recorded ones."""
    nxt = cp.snapshot.get("next_milestone", {})
    _check_primary_axis(cp, nxt, report)
    _check_ruled_out(nxt, report)
    if nxt.get("lora_scope") != "ATTENTION_AND_MLP":
        report.fail("CANDIDATE_STATE",
                    f"LoRA scope {nxt.get('lora_scope')!r} is not the preregistered "
                    f"ATTENTION_AND_MLP")
    if "v2" not in nxt.get("training_corpus", ""):
        report.fail("CANDIDATE_STATE",
                    f"the training corpus for candidate 003 is recorded as "
                    f"{nxt.get('training_corpus')!r}, not train-v2 unchanged")

    baseline = cp.snapshot.get("test_baseline", {})
    if baseline.get("failed") != 0:
        report.fail("CONTROL_PLANE_BUDGET",
                    f"the authoritative baseline records {baseline.get('failed')} "
                    f"failures")
    artifact = baseline.get("known_invocation_artifact", {})
    if artifact.get("is_a_regression") is not False or \
            artifact.get("is_defect_d39") is not False:
        report.fail("CONTROL_PLANE_BUDGET",
                    "the known root-invocation artifact is mislabelled")


# ── Entry point ──────────────────────────────────────────────────────────────────────
def run() -> Report:
    report = Report()
    cp = load(report)
    if cp is None:
        return report
    check_schema(cp, report)
    check_current_pointer(cp, report)
    check_snapshot_chain(cp, report)
    check_archive(cp, report)
    check_paths(cp, report)
    check_git_authority(cp, report)
    check_stale_state(cp, report)
    check_dataset_state(cp, report)
    check_candidate_state(cp, report)
    check_candidate_design(cp, report)
    check_training_receipt(cp, report)
    check_evaluation_receipt(cp, report)
    check_policy_identities(cp, report)
    check_authority_separation(cp, report)
    check_operator_ruling(cp, report)
    check_holdout_firewall(cp, report)
    check_holdout_retirement(cp, report)
    check_record_store(cp, report)
    check_budgets(cp, report)
    check_next(cp, report)
    return report


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the M62 control plane. Offline, read-only, fail-closed.")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress the notes, keep the machine-readable block")
    args = parser.parse_args(argv)

    report = run()

    if not args.quiet:
        for note in report.notes:
            print(f"note: {note}")
    for category, message in report.problems:
        print(f"PROBLEM [{category}] {message}")

    print()
    print(f"M62_CONTROL_PLANE_VERIFY:\n{'PASS' if report.ok else 'FAIL'}")
    # Iterated from CATEGORIES rather than restated: a second literal list is how the
    # machine-readable block silently stops reporting a category somebody added.
    for category in CATEGORIES:
        print(f"{category}:\n{report.status(category)}")
    print(f"PROBLEMS:\n{len(report.problems)}")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
