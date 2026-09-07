"""scripts/migrate_m62_control_plane_v4.py — V69 S5G: Control Plane V3 -> V4.

WHY THIS MIGRATION EXISTS
-------------------------
V3 could not represent its own master integration.

``check_git_authority`` required ``project.master_commit`` to EQUAL the live master
ref. A generation committed onto master therefore declared a value that the act of
committing it invalidated: declare X, commit, master becomes Y, and X != Y. Declare
Y instead and committing that makes Z. The declaration lags the ref it describes by
exactly one commit, permanently, and the only way to close the gap would be for a
commit to contain its own SHA.

Measured, on this repository, before a single line of V4 was written:

  * generation 33 on the S5F branch                  PASS
  * the identical tree checked out as ``master``      FAIL (branch binding)
  * the same, with ``origin/master`` also moved       FAIL (branch AND master_commit)
  * detached HEAD at the integrated commit            FAIL (master_commit; the branch
                                                       half SILENTLY SKIPS itself)
  * a synthetic generation 34, three rounds           FAIL every round, always exactly
                                                       one commit behind

That is a self-reference in the schema, not a Git problem. Git is healthy: master is
a strict ancestor, the fast-forward is available, and the merge simulation is clean.

WHAT V4 CHANGES, AND WHAT IT REFUSES TO CHANGE
----------------------------------------------
V4 changes WHAT IS DECLARED. It does not change the container: the eight
content-addressed record blocks, the record store and the rehydration seam are V3's,
byte for byte. S5G is an AUTHORITY migration, not a representation one.

Three fields move:

  * ``project.master_commit`` is REPLACED by ``integration_authority.integration_base``.
    Same value today, opposite meaning: V3 said "master is here NOW and must stay",
    V4 says "this is the commit the integration was authorised FROM". One is a live
    observation frozen into an immutable record; the other is a historical fact that
    can never go stale.
  * ``project.merged_into_master`` is REMOVED. It was a boolean no check ever verified
    against Git -- false before the push and still false after it -- and it is the
    exact cautionary shape a time-varying fact must not take. Integration status is
    now DERIVED on every run by ``observe_integration_state``.
  * ``project.branch`` SURVIVES as provenance and stops being authority. Under V3 it
    was a gate that skipped itself whenever ``git rev-parse --abbrev-ref HEAD``
    returned "HEAD" -- every detached checkout, which is every ``pull_request`` CI
    run. Lineage is proved by ancestry from the governed subject instead, which is
    identical attached and detached and cannot be bypassed by a checkout shape.

It removes a SELF-REFERENCE. It removes NO TRUTH:

  * every defect, limitation, invariant, dataset, candidate and policy identity is
    carried forward by the SAME digest it had under V3 -- the record store is not
    rewritten, not re-hashed and not touched;
  * the historical master commit is still recorded, still exactly
    ``3705114228edef2f665be349c5c4429b7b16777a``, now as the integration base;
  * "master has not been integrated" is still stated -- as a DERIVED observation
    (``TARGET_AT_AUTHORIZED_BASE``) that is recomputed from live refs, instead of a
    frozen boolean that would silently lie the moment the integration happened;
  * the budget is NOT raised -- ``SNAPSHOT_MAX_BYTES`` stays 34 816;
  * V2 and V3 history (generations 1-33) is NEVER rewritten. Those files keep their
    bytes and their digests, they are still read under V2/V3 semantics, and the chain
    crosses the version boundary by the same parent-digest rule it always used.

THE PROOF OBLIGATION
--------------------
``--verify`` requires, fail-closed:

  1. every V3 record digest appears in the V4 generation UNCHANGED (scientific
     carry-forward is a measurement, not a claim);
  2. no field of the emitted record names a commit that does not already exist --
     the no-self-SHA invariant, checked directly rather than asserted;
  3. the emitted document satisfies BOTH the V4 container schema and the V4 semantic
     schema;
  4. the parent digest is the SHA-256 of generation 33's canonical bytes.

A migration that cannot prove all four is not applied.

NOTHING HERE WRITES UNLESS ``--emit`` IS PASSED, and nothing here reads a held-out
task body, loads a model or touches an adapter.
"""
from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404 - fixed argv, shell=False, read-only git plumbing
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # pragma: no cover - layout shim, as the sibling CLIs do
    sys.path.insert(0, str(_ROOT))

from scripts.verify_m62_control_plane import (  # noqa: E402
    CONTROL_PLANE_V3_SCHEMA_VERSION,
    CONTROL_PLANE_V4_SCHEMA_VERSION,
    INTEGRATION_METHODS,
    INTEGRATION_TARGET_REFS,
    SNAPSHOT_MAX_BYTES,
    V3_RECORD_BLOCKS,
    canonical_bytes,
    load_record_store,
    rehydrate_v3,
    sha256_bytes,
    snapshot_v4_schema,
    snapshot_v4_semantic_schema,
    validate_against_schema,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

STATE_DIR = "state/m62"
SNAPSHOT_DIR = f"{STATE_DIR}/snapshots"
CURRENT_PATH = f"{STATE_DIR}/current.json"

#: The generation V4 succeeds. Named rather than discovered: a migration that picks up
#: "whatever is newest" would silently retarget if it were ever run twice.
PARENT_SNAPSHOT = f"{SNAPSHOT_DIR}/0033-s5f-d39-order-isolation-branch.json"
PARENT_GENERATION = 33

#: The historical master this lineage was authorised to integrate FROM. It is the value
#: generation 33 carried as ``project.master_commit``; V4 keeps the fact and drops the
#: equality demand that made it unsatisfiable.
INTEGRATION_BASE = "3705114228edef2f665be349c5c4429b7b16777a"

TARGET_REF = INTEGRATION_TARGET_REFS[0]
METHOD = INTEGRATION_METHODS[0]

#: Fields the V4 project block keeps. Provenance only.
V4_PROJECT_FIELDS = ("branch", "milestone", "released", "tagged")
#: Fields V4 drops from the project block, and why, for the manifest.
V4_PROJECT_DROPPED = {
    "master_commit": "replaced by integration_authority.integration_base; the equality "
                     "demand it carried is the fixed point",
    "merged_into_master": "removed; a frozen boolean no check verified, now DERIVED as "
                          "an observation",
}


def _git(*args: str) -> "tuple[int, str]":
    try:
        done = subprocess.run(  # nosec B603 - fixed argv list, shell=False, no user input
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True, text=True, timeout=60, check=False, shell=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return done.returncode, done.stdout.strip()


def _load(rel: str) -> "tuple[dict, bytes]":
    raw = (REPO_ROOT / rel).read_bytes()
    return json.loads(raw.decode("utf-8")), raw


def project_v4(parent: dict, *, branch: str, milestone: str) -> dict:
    """The V4 project block: provenance, and nothing that can go stale."""
    out = {"branch": branch, "milestone": milestone}
    for key in ("released", "tagged"):
        out[key] = parent["project"][key]
    return {k: out[k] for k in sorted(V4_PROJECT_FIELDS)}


def project_v4_generation(
        parent: dict, *, subject_commit: str, branch: str, milestone: str,
        label: str, note: str, subject_milestone: str) -> dict:
    """Transform generation 33's stored V3 container into the V4 successor.

    Everything not named here is carried through UNCHANGED, including the entire
    ``records`` map -- the same eight digests, pointing at the same files.
    """
    out = dict(parent)
    out["schema_version"] = CONTROL_PLANE_V4_SCHEMA_VERSION
    out["state_generation"] = PARENT_GENERATION + 1
    out["parent_snapshot_sha256"] = sha256_bytes(_load(PARENT_SNAPSHOT)[1])
    out["subject_state_commit"] = subject_commit
    out["subject_state_milestone"] = subject_milestone
    out["generation_label"] = label
    out["control_plane_note"] = note
    out["project"] = project_v4(parent, branch=branch, milestone=milestone)
    out["integration_authority"] = {
        "governed_subject": subject_commit,
        "integration_base": INTEGRATION_BASE,
        "method": METHOD,
        "target_ref": TARGET_REF,
    }
    return {k: out[k] for k in sorted(out)}


def referenced_commits(payload: dict) -> "list[tuple[str, str]]":
    """Every commit SHA the record names, with the field that names it."""
    authority = payload.get("integration_authority", {})
    found = [("subject_state_commit", payload.get("subject_state_commit", ""))]
    for key in ("integration_base", "governed_subject"):
        found.append((f"integration_authority.{key}", authority.get(key, "")))
    return found


def check_no_self_sha(payload: dict) -> "list[str]":
    """The invariant: no declared commit may fail to already exist.

    A field that named the commit CONTAINING this record could not be satisfied,
    because a commit's SHA is computed from its content. Proving every referenced
    commit already resolves is the direct, executable form of that claim -- it cannot
    be true of a commit that does not exist yet.
    """
    problems = []
    for field, sha in referenced_commits(payload):
        if not sha:
            problems.append(f"{field} is empty")
            continue
        code, kind = _git("cat-file", "-t", sha)
        if code != 0 or kind != "commit":
            problems.append(
                f"{field} names {sha}, which does not already exist as a commit; a V4 "
                f"record may only reference commits that PREDATE it")
    return problems


def check_carried_forward(parent: dict, payload: dict) -> "list[str]":
    """Scientific carry-forward, measured. Empty means nothing moved."""
    problems = []
    for block in V3_RECORD_BLOCKS:
        before = parent.get("records", {}).get(block)
        after = payload.get("records", {}).get(block)
        if before != after:
            problems.append(
                f"record block {block!r} changed digest {before} -> {after}; V4 carries "
                f"the record store forward UNCHANGED")
    for key in ("authority_observation", "next_milestone", "test_baseline"):
        if parent.get(key) != payload.get(key):
            problems.append(f"{key} changed; V4 is an authority migration and moves no "
                            f"scientific fact")
    return problems


def verify(payload: dict, parent: dict) -> "list[str]":
    problems = list(check_carried_forward(parent, payload))
    problems += check_no_self_sha(payload)
    problems += [f"container: {p}"
                 for p in validate_against_schema(snapshot_v4_schema(), payload)]
    # Rehydrate for real, exactly as the verifier will, and validate what it MEANS.
    # A placeholder here would prove the container and nothing about the semantics.
    records = load_record_store(REPO_ROOT / f"{STATE_DIR}/records")
    semantic, rehydration = rehydrate_v3(
        payload, records, CONTROL_PLANE_V4_SCHEMA_VERSION)
    problems += [f"rehydration: {p}" for p in rehydration]
    if not rehydration:
        problems += [f"semantic: {p}" for p in validate_against_schema(
            snapshot_v4_semantic_schema(), semantic)]
    expected_parent = sha256_bytes(_load(PARENT_SNAPSHOT)[1])
    if payload.get("parent_snapshot_sha256") != expected_parent:
        problems.append(f"parent_snapshot_sha256 must be {expected_parent}")
    size = len(canonical_bytes(payload))
    if size > SNAPSHOT_MAX_BYTES:
        problems.append(f"{size} bytes, over {SNAPSHOT_MAX_BYTES}")
    return problems


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--subject-commit", required=True,
                        help="the commit S5G closed at; must ALREADY EXIST")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--milestone", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--note", required=True)
    parser.add_argument("--subject-milestone", default="S5G")
    parser.add_argument("--out", required=True,
                        help=f"path under {SNAPSHOT_DIR}/ to write")
    parser.add_argument("--emit", action="store_true",
                        help="write the generation and repoint current.json")
    args = parser.parse_args(argv)

    parent, parent_raw = _load(PARENT_SNAPSHOT)
    if parent.get("schema_version") != CONTROL_PLANE_V3_SCHEMA_VERSION:
        print(f"S5G_MIGRATION_REFUSED: {PARENT_SNAPSHOT} is not a V3 generation")
        return 1
    if parent.get("state_generation") != PARENT_GENERATION:
        print(f"S5G_MIGRATION_REFUSED: parent is generation "
              f"{parent.get('state_generation')}, expected {PARENT_GENERATION}")
        return 1

    payload = project_v4_generation(
        parent, subject_commit=args.subject_commit, branch=args.branch,
        milestone=args.milestone, label=args.label, note=args.note,
        subject_milestone=args.subject_milestone)

    problems = verify(payload, parent)
    for problem in problems:
        print(f"PROBLEM {problem}")
    if problems:
        print("\nS5G_CONTROL_PLANE_V4_MIGRATION:\nREFUSED")
        return 1

    data = canonical_bytes(payload)
    print(f"generation {payload['state_generation']} — {len(data)} bytes, "
          f"{SNAPSHOT_MAX_BYTES - len(data)} spare")
    print(f"parent digest {payload['parent_snapshot_sha256']}")
    print(f"record blocks carried forward unchanged: {len(V3_RECORD_BLOCKS)}")
    for field, sha in referenced_commits(payload):
        print(f"  references {field} = {sha} (already exists)")

    if args.emit:
        out = REPO_ROOT / args.out
        out.write_bytes(data)
        current, _ = _load(CURRENT_PATH)
        current["latest_snapshot_path"] = args.out
        current["latest_snapshot_sha256"] = sha256_bytes(data)
        current["schema_version"] = CONTROL_PLANE_V4_SCHEMA_VERSION
        current["state_generation"] = payload["state_generation"]
        current["subject_state_commit"] = payload["subject_state_commit"]
        (REPO_ROOT / CURRENT_PATH).write_bytes(
            canonical_bytes({k: current[k] for k in sorted(current)}))
        print(f"\nwrote {args.out}\nrepointed {CURRENT_PATH}")
    else:
        print("\n(dry run — pass --emit to write)")

    print("\nS5G_CONTROL_PLANE_V4_MIGRATION:\nPASS")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
