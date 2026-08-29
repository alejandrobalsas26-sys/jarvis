"""scripts/migrate_m62_control_plane_v3.py — V69 M63: Control Plane V2 -> V3.

WHY THIS MIGRATION EXISTS
-------------------------
Generation 15 closed at 33 742 bytes against a 34 816-byte snapshot budget:
**1 074 bytes of headroom**, against a policy floor of 1 024. Fifty bytes of
real slack, and the generation-15 ``ruled_out`` bars raising a reviewed budget
or deleting recorded defects, limitations or invariants to make room.

Three measurements decided this, not a preference:

  * the smallest truthful generation 16 — a branch declaration that adds NO
    scientific fact — measures 33 842 bytes, leaving 974. Already blocked.
  * a candidate-005 ``DESIGNED_UNTRAINED`` entry costs **505 bytes of
    irreducible structured data** (identity, corpus, evidence path, the null
    hash fields). Parent + entry alone = 34 247 bytes, leaving 569, before a
    single word of note, label, invariant or ``next_milestone`` update.
  * the later ``TRAINED_UNEVALUATED`` state replaces three of those nulls with
    two 64-character digests and a receipt path, and is larger still.

So V2 cannot hold the next truthful state by a margin no amount of rewording
closes. That is the precondition the roadmap set for a V3, and it is met.

WHAT V3 CHANGES, AND WHAT IT REFUSES TO CHANGE
----------------------------------------------
84% of a snapshot (28 231 of 33 742 bytes at generation 15) is blocks that are
immutable or strictly append-only, re-serialised in full every generation:
``defects`` 7 385, ``limitations`` 7 210, ``frozen_invariants`` 4 722,
``datasets`` 4 487, ``candidates`` 2 892, plus ``policy_identities``,
``base_model`` and ``archive``.

V3 moves those into CONTENT-ADDRESSED RECORDS under ``state/m62/records/`` and
has the generation reference each by digest. The generation itself keeps only
what is genuinely per-generation: the label, the subject commit, the project
block, the note, the authority observation, the next milestone and the test
baseline.

It removes DUPLICATION. It removes NO TRUTH:

  * every defect, limitation and invariant is preserved byte for byte;
  * no hash is truncated, no evidence path dropped;
  * the budget is NOT raised — ``SNAPSHOT_MAX_BYTES`` stays 34 816, and a V3
    generation simply fits inside it with room to grow;
  * V2 history (generations 1-15) is NEVER rewritten. Those files keep their
    bytes and their digests, and the chain crosses the version boundary by the
    same parent-digest rule it always used.

THE PROOF OBLIGATION
--------------------
``--verify`` rehydrates a V3 snapshot back to its V2 shape and requires the
result to be BYTE-IDENTICAL to the V2 document under the one canonical
serialization. A migration that cannot prove that is not applied.

NOTHING HERE WRITES UNLESS ``--emit`` IS PASSED, and nothing here reads a
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
    SNAPSHOT_MAX_BYTES,
    canonical_bytes,
    sha256_bytes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

CONTROL_PLANE_V2_SCHEMA = "m62.control_plane.1"
CONTROL_PLANE_V3_SCHEMA = "m62.control_plane.3"

STATE_DIR = "state/m62"
RECORD_DIR = f"{STATE_DIR}/records"

#: The policy floor a projected generation must still clear. Unchanged from V2:
#: V3 earns headroom by architecture, not by moving the line.
REQUIRED_HEADROOM_BYTES = 1024

#: The blocks that become content-addressed records. Chosen because each one is
#: immutable or append-only and is re-serialised verbatim across generations.
#: ``next_milestone`` and ``test_baseline`` are deliberately NOT here: they
#: change every generation, so addressing them would add a digest and save
#: nothing.
RECORD_BLOCKS = (
    "archive",
    "base_model",
    "candidates",
    "datasets",
    "defects",
    "frozen_invariants",
    "limitations",
    "policy_identities",
)

#: Kept inline in the generation. The union of these and RECORD_BLOCKS must be
#: exactly the V2 key set, which :func:`check_partition` asserts.
INLINE_BLOCKS = (
    "authority_observation",
    "control_plane_note",
    "generation_label",
    "next_milestone",
    "parent_snapshot_sha256",
    "project",
    "schema_version",
    "state_generation",
    "subject_state_commit",
    "subject_state_milestone",
    "test_baseline",
)


class MigrationError(RuntimeError):
    """A migration that will not be performed as described."""


def check_partition(v2: dict) -> None:
    """The two block lists must exactly partition the V2 document.

    This is the whole safety argument in one assertion: if every V2 key is in
    exactly one of the lists, then rehydration can reconstruct the document,
    and nothing can be silently dropped by being in neither.
    """
    declared = set(RECORD_BLOCKS) | set(INLINE_BLOCKS)
    actual = set(v2)
    missing = actual - declared
    if missing:
        raise MigrationError(
            f"V2 keys {sorted(missing)} are in neither RECORD_BLOCKS nor "
            f"INLINE_BLOCKS. A key in neither list is a key the migration would "
            f"DROP, which is the one thing it may never do")
    unknown = declared - actual
    if unknown:
        raise MigrationError(
            f"the migration declares keys {sorted(unknown)} that the V2 document "
            f"does not have; the partition describes a different document")
    overlap = set(RECORD_BLOCKS) & set(INLINE_BLOCKS)
    if overlap:
        raise MigrationError(f"keys {sorted(overlap)} are in BOTH lists")


def record_digest(block_name: str, value: object) -> str:
    """The content address of one block.

    The block NAME is inside the hashed bytes, so two blocks that happen to
    serialise identically (an empty ``defects`` and an empty ``limitations``,
    say) still get distinct addresses and cannot be confused for one another.
    """
    return sha256_bytes(canonical_bytes({"block": block_name, "value": value}))


def record_payload(block_name: str, value: object) -> dict:
    return {"block": block_name, "value": value}


def to_v3(v2: dict) -> tuple[dict, dict[str, dict]]:
    """Split a V2 snapshot into a V3 generation plus its records.

    Returns ``(v3_snapshot, {digest: record_payload})``. Pure: writes nothing.
    """
    check_partition(v2)
    records: dict[str, dict] = {}
    reference: dict[str, str] = {}
    for name in RECORD_BLOCKS:
        payload = record_payload(name, v2[name])
        digest = record_digest(name, v2[name])
        records[digest] = payload
        reference[name] = digest

    v3 = {key: v2[key] for key in INLINE_BLOCKS}
    v3["schema_version"] = CONTROL_PLANE_V3_SCHEMA
    v3["records"] = dict(sorted(reference.items()))
    return v3, records


def rehydrate(v3: dict, records: dict[str, dict]) -> dict:
    """Reconstruct the V2-shaped document from a V3 generation and its records.

    Fails closed on a missing record, a record whose stored bytes do not hash to
    the digest that referenced it, and a record whose ``block`` disagrees with
    the reference that named it. Each of those would otherwise let a generation
    quietly mean something other than what it says.
    """
    out = {key: v3[key] for key in INLINE_BLOCKS if key in v3}
    out["schema_version"] = CONTROL_PLANE_V2_SCHEMA
    reference = v3.get("records")
    if not isinstance(reference, dict):
        raise MigrationError("the V3 snapshot carries no records map")
    if set(reference) != set(RECORD_BLOCKS):
        raise MigrationError(
            f"the records map names {sorted(reference)}, the contract names "
            f"{sorted(RECORD_BLOCKS)}")
    for name, digest in reference.items():
        payload = records.get(digest)
        if payload is None:
            raise MigrationError(f"record {digest} for block {name!r} is missing")
        measured = sha256_bytes(canonical_bytes(payload))
        if measured != digest:
            raise MigrationError(
                f"record for block {name!r} hashes to {measured}, but was "
                f"referenced as {digest}")
        if payload.get("block") != name:
            raise MigrationError(
                f"record {digest} says it is block {payload.get('block')!r} but "
                f"was referenced as {name!r}")
        out[name] = payload["value"]
    return out


def load_records(directory: Path) -> dict[str, dict]:
    """Read the record store. The FILENAME is not trusted as the address."""
    out: dict[str, dict] = {}
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_bytes().decode("utf-8"))
        out[sha256_bytes(canonical_bytes(payload))] = payload
    return out


def prove_equivalence(v2: dict) -> tuple[bool, str]:
    """V2 -> V3 -> V2 must return the ORIGINAL BYTES, not merely equal data."""
    v3, records = to_v3(v2)
    try:
        restored = rehydrate(v3, records)
    except MigrationError as exc:
        return False, f"rehydration failed: {exc}"
    original, roundtrip = canonical_bytes(v2), canonical_bytes(restored)
    if original != roundtrip:
        return False, (f"round-trip differs: {len(original)} bytes in, "
                       f"{len(roundtrip)} bytes out")
    return True, f"byte-identical over {len(original)} bytes"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Control Plane V2 -> V3. Proves semantic equivalence; writes "
                    "nothing unless --emit is passed.")
    parser.add_argument("--snapshot", default="",
                        help="the V2 snapshot to migrate. Defaults to whatever "
                             "state/m62/current.json points at")
    parser.add_argument("--emit-records", default="",
                        help="directory to write the content-addressed records into")
    parser.add_argument("--emit-snapshot", default="",
                        help="path to write the V3 generation to")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.snapshot:
        snapshot_path = Path(args.snapshot)
    else:
        current = json.loads(
            (REPO_ROOT / STATE_DIR / "current.json").read_text("utf-8"))
        snapshot_path = REPO_ROOT / current["latest_snapshot_path"]

    v2_bytes = snapshot_path.read_bytes()
    v2 = json.loads(v2_bytes.decode("utf-8"))
    if v2.get("schema_version") != CONTROL_PLANE_V2_SCHEMA:
        print(f"NOT_A_V2_SNAPSHOT: {v2.get('schema_version')}")
        return 2

    try:
        v3, records = to_v3(v2)
    except MigrationError as exc:
        print(f"MIGRATION_REFUSED: {exc}")
        return 1

    ok, detail = prove_equivalence(v2)
    v3_bytes = canonical_bytes(v3)
    headroom = SNAPSHOT_MAX_BYTES - len(v3_bytes)
    record_bytes = sum(len(canonical_bytes(r)) for r in records.values())

    results = {
        "source_snapshot": str(snapshot_path.relative_to(REPO_ROOT)),
        "source_generation": v2.get("state_generation"),
        "v2_bytes": len(v2_bytes),
        "v2_headroom": SNAPSHOT_MAX_BYTES - len(v2_bytes),
        "v3_bytes": len(v3_bytes),
        "v3_headroom": headroom,
        "records": len(records),
        "record_store_bytes": record_bytes,
        "bytes_saved_in_snapshot": len(v2_bytes) - len(v3_bytes),
        "budget_unchanged": SNAPSHOT_MAX_BYTES,
        "required_headroom": REQUIRED_HEADROOM_BYTES,
        "semantic_equivalence": "PASS" if ok else "FAIL",
        "semantic_equivalence_detail": detail,
        "capacity": "PASS" if headroom >= REQUIRED_HEADROOM_BYTES else "BLOCKED",
    }
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        for key in sorted(results):
            print(f"{key.upper()}: {results[key]}")

    if not ok:
        print("V2_TO_V3_SEMANTIC_EQUIVALENCE: FAIL")
        return 1
    print("V2_TO_V3_SEMANTIC_EQUIVALENCE: PASS")

    if args.emit_records or args.emit_snapshot:
        if not (args.emit_records and args.emit_snapshot):
            print("EMIT_REFUSED: --emit-records and --emit-snapshot go together; "
                  "a generation without its records is unreadable")
            return 1
        if headroom < REQUIRED_HEADROOM_BYTES:
            print("EMIT_REFUSED: the capacity gate did not pass")
            return 1
        out_dir = Path(args.emit_records)
        out_dir.mkdir(parents=True, exist_ok=True)
        for digest, payload in sorted(records.items()):
            (out_dir / f"{digest}.json").write_bytes(canonical_bytes(payload))
        Path(args.emit_snapshot).write_bytes(v3_bytes)
        print(f"EMITTED_RECORDS: {len(records)} -> {args.emit_records}")
        print(f"EMITTED_SNAPSHOT: {args.emit_snapshot}")
        print(f"EMITTED_SNAPSHOT_SHA256: {sha256_bytes(v3_bytes)}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
