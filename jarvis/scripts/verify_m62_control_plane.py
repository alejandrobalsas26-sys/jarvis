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

STATE_DIR = "state/m62"
CURRENT_PATH = f"{STATE_DIR}/current.json"
SNAPSHOT_DIR = f"{STATE_DIR}/snapshots"
SCHEMA_DIR = f"{STATE_DIR}/schema"
CURRENT_SCHEMA_PATH = f"{SCHEMA_DIR}/m62-current.schema.json"
SNAPSHOT_SCHEMA_PATH = f"{SCHEMA_DIR}/m62-snapshot.schema.json"
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

#: Files that hold `eval-v4` task BODIES. A control-plane surface may never cite one as
#: an evidence pointer, and a candidate-003 design session may never open one.
FORBIDDEN_BODY_SOURCES = (
    "jarvis/scripts/build_evaluation_corpus.py",
    "task-pack.jsonl",
)
FORBIDDEN_BODY_SYMBOLS = ("corpus_v4_material", "corpus_v4(")

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

#: No promotion authority exists in this repository — ``ModelCandidateProposal`` is
#: non-effectful by construction and mutates no registry. A snapshot that RECORDS a
#: promoted candidate is therefore recording something no repository artefact can
#: witness, and is refused.
UNWITNESSABLE_CANDIDATE_STATES = ("PROMOTED",)

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
    "m62-defensive-eval v4": (
        "FROZEN_UNUSED",
        "8c6871b0094bdfc75062a6352d383fa8e9750c1425182a2b3248db20500081c5"),
    "m62-defensive-quality-train v1": (
        "USED_IMMUTABLE",
        "9bbac2f057fd0592a30a7fdeb968655f8ea585df00966e1b920415377ab7286a"),
    "m62-defensive-quality-train v2": (
        "USED_IMMUTABLE",
        "24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f"),
}

#: ``candidate_id -> (status, adapter_sha256 or None)``
FROZEN_CANDIDATES: dict[str, tuple[str, "str | None"]] = {
    "qwen3-06b-lora-quality-live-001": (
        "EVALUATED_NOT_ELIGIBLE",
        "43213035c15cd38928d2d6a3bdbd9af96872a954801c6bfd0a9b82a8e22ac858"),
    "qwen3-06b-lora-quality-live-002": (
        "EVALUATED_NOT_ELIGIBLE",
        "319c252498ba51e01ed59f58fc20ae639e2d886bf67277d3aa6df2e9f9665409"),
    # Candidate 003 has NO adapter identity and no run name. Inventing one here would be
    # a candidate-design decision, and S3N.1 is forbidden from making it.
    "candidate-003": ("NOT_CREATED", None),
}

#: ``defect id -> status``, as the milestone that closed or opened it recorded.
FROZEN_DEFECT_STATUSES: dict[str, str] = {
    "D37": "FIXED",
    "D38": "FIXED_OBSERVABILITY_ONLY",
    "D39": "OPEN",
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
SNAPSHOT_MAX_BYTES = 32_768
# current.json migrated at 398 bytes. A pointer that needs a kilobyte is a second state.
CURRENT_MAX_BYTES = 2_048
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
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _type_name(value: object) -> str:
    for name in ("null", "boolean", "integer", "string", "array", "object"):
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
            "schema_version": {"const": CONTROL_PLANE_SCHEMA_VERSION},
            "state_generation": {"type": "integer", "minimum": 1, "maximum": 100_000},
            "latest_snapshot_path": _REPO_PATH,
            "latest_snapshot_sha256": _SHA256,
            "subject_state_commit": _COMMIT,
            "verify_command": _SHORT,
        }),
    }


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
    })
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
    "DATASET_STATE", "CANDIDATE_STATE", "POLICY_IDENTITIES", "AUTHORITY_SEPARATION",
    "HOLDOUT_FIREWALL", "PATH_INTEGRITY", "STALE_STATE", "CONTROL_PLANE_BUDGET",
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
    snapshot: dict
    snapshot_bytes: bytes
    snapshot_path: Path
    migration: dict


def _load_json(path: Path, label: str) -> "tuple[dict, bytes]":
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"S3N1_CONTROL_PLANE_UNREADABLE: {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"S3N1_CONTROL_PLANE_UNREADABLE: {label} is not an object")
    return payload, raw


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
    return ControlPlane(current, current_raw, snapshot, snapshot_raw, snapshot_file,
                        migration)


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


# ── Checks ───────────────────────────────────────────────────────────────────────────
def check_schema(cp: ControlPlane, report: Report) -> None:
    """V1, V2, V28 — both documents strictly valid; unknown keys refused."""
    for label, schema, payload in (
            ("current.json", current_schema(), cp.current),
            ("snapshot", snapshot_schema(), cp.snapshot)):
        for problem in validate_against_schema(schema, payload):
            report.fail("SCHEMA", f"{label}: {problem}")

    # The published schema files must be the ones this verifier enforces. Two writable
    # copies of one contract is how they drift.
    for rel, builder in ((CURRENT_SCHEMA_PATH, current_schema),
                         (SNAPSHOT_SCHEMA_PATH, snapshot_schema)):
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
    if cp.current.get("schema_version") != cp.snapshot.get("schema_version"):
        report.fail("CURRENT_POINTER", "schema_version differs between the two documents")

    # The snapshot on disk must already BE in canonical form, so the digest above is the
    # digest of the file a human can read.
    if cp.snapshot_bytes != canonical_bytes(cp.snapshot):
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
        previous = {c.get("candidate_id"): c.get("status")
                    for c in parent.get("candidates", [])}
        for cid, entry in by_id.items():
            before = previous.get(cid)
            after = entry.get("status")
            if before is None or before == after:
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
    generation = cp.snapshot.get("state_generation", 1)
    if generation <= 1:
        return None
    directory = REPO_ROOT / SNAPSHOT_DIR
    for path in sorted(directory.iterdir()):
        if path.suffix == ".json" and path.name.startswith(f"{generation - 1:04d}-"):
            payload, _ = _load_json(path, path.name)
            return payload
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


def check_holdout_firewall(cp: ControlPlane, report: Report) -> None:
    """V23, V24 — no task body, no body source pointer, no secret, no private path.

    The firewall is enforced WITHOUT opening a single `eval-v4` body. Three structural
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

        named = sorted({tid for tid in EVAL_V4_TASK_IDS if tid in text})
        if named:
            report.fail("HOLDOUT_FIREWALL",
                        f"{rel} names individual eval-v4 task(s) {named[:4]}; a routine "
                        f"bootstrap surface has no reason to carry per-task material")
        for symbol in FORBIDDEN_BODY_SYMBOLS:
            if symbol in text:
                report.fail("HOLDOUT_FIREWALL",
                            f"{rel} references {symbol!r}, the eval-v4 body source")
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


def check_next(cp: ControlPlane, report: Report) -> None:
    """V26, V27 — the NEXT contract and the test baseline are the recorded ones."""
    nxt = cp.snapshot.get("next_milestone", {})
    if "MODEL_DEFAULT" not in nxt.get("primary_axis", "") or \
            "DISABLED" not in nxt.get("primary_axis", ""):
        report.fail("CANDIDATE_STATE",
                    f"the preregistered primary axis is not recorded: "
                    f"{nxt.get('primary_axis')!r}")
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
    check_policy_identities(cp, report)
    check_authority_separation(cp, report)
    check_holdout_firewall(cp, report)
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
    for category in ("SCHEMA", "CURRENT_POINTER", "SNAPSHOT_CHAIN", "ARCHIVE_INTEGRITY",
                     "GIT_AUTHORITY", "DATASET_STATE", "CANDIDATE_STATE",
                     "POLICY_IDENTITIES", "AUTHORITY_SEPARATION", "HOLDOUT_FIREWALL",
                     "PATH_INTEGRITY", "STALE_STATE", "CONTROL_PLANE_BUDGET"):
        print(f"{category}:\n{report.status(category)}")
    print(f"PROBLEMS:\n{len(report.problems)}")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
