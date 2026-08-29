# V69 M63 S4A — Control Plane V3 (content-addressed records)

## Why this migration happened

V2 could not hold the next truthful generation. Three measurements, not a preference:

| Projection | Bytes | Headroom | Policy floor 1024 |
|---|---|---|---|
| generation 15 (sealed) | 33,742 | 1,074 | 50 bytes of real slack |
| generation 16, branch declaration + truthful note | 33,842 | 974 | **BLOCKED** |
| candidate-005 `DESIGNED_UNTRAINED`, **entry alone** | 34,247 | 569 | **BLOCKED by 455** |

The candidate entry is 505 bytes of irreducible structured data — identity, base
revision, corpus, evidence path, the null hash fields — before a single word of note,
label, invariant or `next_milestone` update. The later `TRAINED_UNEVALUATED` state
replaces three of those nulls with two 64-character digests and a receipt path, and is
larger still.

Generation 15's `ruled_out` bars the two easy exits: *"raising a reviewed budget, or
deleting recorded defects, limitations or invariants to make room."* So the only
remaining move was architectural.

## What V3 changes

At generation 15, **28,231 of 33,742 bytes (84%)** sat in blocks that are immutable or
strictly append-only and were re-serialised in full every generation:

| Block | Bytes |
|---|---|
| `defects` | 7,385 |
| `limitations` | 7,210 |
| `frozen_invariants` | 4,722 |
| `datasets` | 4,487 |
| `candidates` | 2,892 |
| `policy_identities` | 801 |
| `base_model` | 504 |
| `archive` | 230 |

V3 moves those into content-addressed records under `state/m62/records/`, each file named
for the sha256 of its own canonical bytes. The generation references each by digest.

Result: generation 16 is **6,186 bytes with 28,630 of headroom**, against a budget that
was **not raised** — `SNAPSHOT_MAX_BYTES` is still 34,816.

## What V3 does not change

It removes duplication. It removes no truth.

- Every defect, limitation and invariant is preserved byte for byte.
- No hash is truncated, no evidence path dropped, no entry reordered away.
- The budget is unchanged; the snapshot simply got smaller.
- **V2 history is never rewritten.** Generations 1–15 keep their exact bytes and digests.
  A test asserts all fifteen are still V2 and still canonical.
- The chain crosses the version boundary by the same rule it always used: generation 16's
  `parent_snapshot_sha256` is the sha256 of generation 15's bytes.

## The proof obligation

`V2 → V3 → V2` must return the **original bytes** under the one canonical serialization,
for **every sealed generation** — not just the one migrated.

```
V2_TO_V3_SEMANTIC_EQUIVALENCE: PASS   (byte-identical over 33,742 bytes)
```

`scripts/migrate_m62_control_plane_v3.py --emit-*` refuses to write if that proof fails or
if the capacity gate does not pass.

## How the verifier handles two formats

`load()` rehydrates a V3 generation into its V2 shape **before any check runs**. Every one
of the existing checks is therefore written against one shape and is unaware the format
changed — the migration did not require editing sixty checks, which is exactly how a
format change introduces a semantic one.

Three checks needed the stored form rather than the rehydrated one, and say so:

- canonical-serialization (measures the bytes on disk),
- `current.json` ↔ snapshot `schema_version` agreement,
- the new `RECORD_STORE` category.

A V3 generation must satisfy **both** contracts: the stored container against
`m62-snapshot-v3.schema.json`, and the rehydrated semantics against the unchanged
`m62-snapshot.schema.json`. That pairing is what makes the change representation-only.

## Tamper-evidence

`RECORD_STORE` requires every referenced record to exist, be tracked in Git, carry no
executable bit, be a regular file, be in canonical form, and **hash to the digest that
referenced it**. `rehydrate_v3` re-hashes rather than trusting the map key — a test
(`test_a_tampered_record_stops_resolving`) found that gap during development and it was
closed rather than the test weakened.

A record that is edited stops resolving. A record referenced under the wrong block name is
refused. A records map missing a block is refused.

## Scope boundary

This migration moved **no scientific state**. Asserted by test:

- candidate 004 stays `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` under the S3Z **HOLD**;
- eval-v6 stays `USED_IMMUTABLE`; eval-v5 stays `FROZEN_UNUSED`, `spent_by` null, RETIRED;
- no candidate 005 exists;
- `authority_observation` is `NONE_OBSERVED_IN_REPOSITORY` for EVAL, TRAIN and promotion,
  and `control_plane_can_grant_authority` is `false`.

Generation 16 also declares the development branch `jarvis-v69-m63-world-state`, which is
the other fact that changed and the reason `GIT_AUTHORITY` needed a new generation at all.

## Evidence

| | |
|---|---|
| Migration tool | `jarvis/scripts/migrate_m62_control_plane_v3.py` |
| Migration tests | `jarvis/tests/test_control_plane_v3_migration.py` |
| Manifest | `state/m62/migrations/0002-control-plane-v3.json` |
| Schema | `state/m62/schema/m62-snapshot-v3.schema.json` |
| Generation | `state/m62/snapshots/0016-m63-control-plane-v3-and-branch.json` |
| Records | `state/m62/records/` (8) |

This document is evidence, not authority. It grants nothing. The snapshot is the state,
and the verifier is what checks it.
