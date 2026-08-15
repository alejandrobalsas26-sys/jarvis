# V69 M62 S3N.1 — Control Plane V2 and the zero-trust handoff migration

> **Status: `S3N1_CONTROL_PLANE_V2: PASS`.** The monolithic `PROGRESS.md` is archived
> byte-for-byte, current state is a strict machine-readable snapshot in an append-only
> hash chain, and a fail-closed offline verifier stands in front of both.
> **Zero training, zero evaluation, zero model generations, zero optimizer steps, no
> `TRAIN` or `EVAL` authority, no candidate 003, no `train-v3`, no `eval-v4` task body
> read.**

| | |
|---|---|
| Milestone | V69 M62 **S3N.1** — control-plane and documentation infrastructure only |
| Date | 2026-08-15 |
| Branch | `jarvis-v69-m62-training-gym` |
| Starting HEAD | `ec446e348995acb0c23a69b0c3efd574f821b1a0` (S3N close) |
| Master | `3705114228edef2f665be349c5c4429b7b16777a` (untouched) |
| Subject state commit | `ec446e348995acb0c23a69b0c3efd574f821b1a0` |
| Preceding milestone | S3N — `V69_M62_S3N_FRESH_EVAL_V4_FREEZE.md` |

---

## 1 — Purpose

Two problems, and the migration had to solve both without trading one for the other.

**Scalability.** `PROGRESS.md` had grown to **6,089 lines / 516,784 bytes**, and it was a
mandatory read at the start of every coding-agent session. Its own closing line asked for
it to be kept "under roughly 1000 lines"; it was six times that. The document's update
protocol told each milestone to append, so the cost grew monotonically.

**Trust.** Everything an agent knew about M62 arrived as prose, and prose has no error
bars. Two failure modes follow directly, and both already have precedent in this
milestone's own defect ledger:

* an agent reads a **superseded** sentence as current — the D32 shape, where a recorded
  digest reproduced nowhere and would have been discovered at the exact moment a
  single-use `EVAL` token was about to be spent;
* an agent reads a **description** of authority as a **grant** of it.

So the migration had to produce **less context** and **more verifiability** at once.

Measured result: the routine bootstrap surface fell from **516,784 bytes** of mandatory
reading to **46,552 bytes** across three files — a **90.99 %** reduction — while the number
of independently cross-checked facts went from zero to thirty.

---

## 2 — What changed, and what deliberately did not

| | |
|---|---|
| **Changed** | `PROGRESS.md` (rewritten as a control plane), plus eight new files |
| **New** | the archive, the state tree, two schemas, the genesis snapshot, the migration manifest, the history index, the verifier, the test file |
| **Unchanged** | **every file under `training_gym/`**, every milestone document, every dataset, every adapter, every gate, grader, metric, threshold and policy |

```
PRODUCTION_SOURCE_CHANGED:        NO
TRAINING_GYM_FILES_CHANGED:       0
EXISTING_MILESTONE_DOCS_CHANGED:  0
EXISTING_TESTS_CHANGED:           0
```

No pre-existing test was edited or weakened. That is worth stating plainly: a migration of
this size that had to adjust existing assertions would be changing behaviour, not
representation.

---

## 3 — Threat model

Bounded, and deliberately modest about what it defeats. **This is not a security boundary
against an attacker who can rewrite the repository.** An adversary able to edit the
snapshot, the archive, the verifier and the tests *together* defeats all of it. Git — a
content-addressed history with a remote — is what makes such a rewrite visible. What this
architecture defeats is the realistic failure: something changing one of those things and
not the others, whether by an agent's mistake or a human's.

| # | Asset | Failure mode | Detection | Prevention | Residual risk |
|---|---|---|---|---|---|
| T1 | current state | an agent hallucinates it | thirty cross-checked claims; the verifier re-derives rather than reads | a typed snapshot with a closed vocabulary | facts with no independent source (e.g. an adapter's existence) are still claims |
| T2 | current state | superseded history read as current | history is physically separated into the archive; `PROGRESS.md` holds no event log | the bootstrap contract excludes the archive by default | an agent may still choose to read the archive and misdate it |
| T3 | the archive | an immutable artefact is rewritten | SHA-256 pinned by two witnesses, compared against the bytes on every run | append-never-edit rule; the verifier refuses on mismatch and forbids "update the hash" | none within Git; a rewrite plus a matching push is visible in history |
| T4 | `eval-v4` | a frozen holdout is edited | manifest, parent and pack digests pinned; task count pinned | the immutability rule and the S3N freeze | the corpus itself is a gitignored runtime artefact and is not re-hashed here |
| T5 | `eval-v4` | its bodies leak into candidate design | no bootstrap surface may name a task, cite a body source, or hold a body-shaped key | four structural rules, none of which require reading the bodies | a determined agent can still open the generator directly |
| T6 | authority | prose claims TRAIN/EVAL authorisation | authority-shaped keys refused; token-shaped values refused; grant-shaped prose flagged | the control plane mints nothing; capability lives in a single-use plan token | a human can still spend a token for the wrong reason |
| T7 | state JSON | manually edited | strict schema, closed enums, hash-chained, cross-checked against Git and the production classes | canonical serialization; the file's digest is the file | an edit consistent across snapshot, manifest and Git passes |
| T8 | the archive | manually edited | as T3 | as T3 | as T3 |
| T9 | pointer/snapshot | they drift apart | the pointer's digest is recomputed from the snapshot bytes | one pointer, one chain | none |
| T10 | state machines | an illegal transition | closed transition tables; every jump absent from them is refused | `EVALUATED_NOT_ELIGIBLE` and `USED_IMMUTABLE` are terminal | a single-generation edit that rewrites both endpoints |
| T11 | statuses | an unknown value read optimistically | closed enums; `UNKNOWN` is a real value | no `missing -> clean` default anywhere | none in the schema; upstream data may still be wrong |
| T12 | portability | a host path leaks into state | private-path regex plus the repository's own leak scanner over every surface | no absolute path is ever written into state | none measured |
| T13 | secrets | a secret enters a control-plane document | `core.redaction_policy.scan_for_leaks` over every surface | JSON string-length cap; closed key sets | the scanner's own coverage |
| T14 | integrity | a self-referential hash design | the snapshot never contains its own digest | the pointer holds it, and the verifier recomputes it | none |
| T15 | freshness | a state-bearing milestone leaves the plane stale | tracked state-bearing paths diffed `subject..HEAD`; `PROGRESS` must cite the pointer | the milestone-close rule | **PARTIAL** — gitignored runtime artefacts cannot be diffed (§13) |
| T16 | recovery | an agent "repairs" a failing verifier | the failure text names the remediation as operator review | documented fail-closed recovery | a sufficiently determined agent can still edit the verifier |
| T17 | concurrency | two agents write one generation | duplicate and skipped generations are refused | one-writer rule, checked post-hoc and documented pre-write | two agents could each produce a *different* generation number |
| T18 | history | a force-push rewrites it | outside this architecture — Git and the remote own it | branch discipline; `master` untouched and verified | not defended here, and not claimed to be |
| T19 | history | a milestone is compacted away | every archive section is asserted present in the coverage table | the byte-identical archive | none |
| T20 | `eval-v4` | body-free identity replaced by bodies | body-shaped keys and long strings refused in state | the schema's string cap | prose surfaces rely on the task-id rule |

**No security theatre.** There is no blockchain, no Merkle tree, no HMAC with a hard-coded
key, no signature, no local "secret" beside the verifier, and no encryption with an
embedded key. The hash chain exists for agent-level state verification and portable tamper
evidence, and for nothing else. Commit signing and external key infrastructure are a
possible future hardening layer, deliberately **not** introduced here: they would add cloud
or credential dependencies that this milestone has no authority to take on.

---

## 4 — Old architecture

One file mixed eight roles:

```
CURRENT STATE + EVENT HISTORY + DECISIONS + INVARIANTS + AUTHORITY HANDOFF
+ TEST BASELINES + NEXT + SUPERSEDED HISTORY
```

It worked during rapid development, for a real reason: everything was in one place and
nothing could be missed. It does not scale, because the two design principles it violates
are the ones that make state cheap:

```
CURRENT STATE != EVENT LOG
DOCUMENTATION != CAPABILITY
```

---

## 5 — New architecture

Five roles, five homes, and they must not collapse again.

| Role | Home | Property |
|---|---|---|
| CURRENT control state | `PROGRESS.md`, `state/m62/current.json`, the latest snapshot | small, verified, rewritten each state-bearing milestone |
| DEEP authority | `jarvis/docs/V69_M62_*.md` | immutable per milestone, read on demand |
| HISTORICAL event log | `jarvis/docs/m62/history/PROGRESS_THROUGH_S3N.md` | append-never-edit, byte-identical |
| NAVIGATION | `jarvis/docs/m62/HISTORY_INDEX.md` | routes, never repeats |
| TRUST BOUNDARY | `jarvis/scripts/verify_m62_control_plane.py` | offline, read-only, fail-closed |

```
PROGRESS.md                                          510 lines,  27,013 B
state/m62/current.json                                            398 B
state/m62/snapshots/0001-m62-control-plane-v2-genesis.json     19,141 B
state/m62/schema/m62-current.schema.json                        1,236 B
state/m62/schema/m62-snapshot.schema.json                      16,463 B
state/m62/migrations/0001-control-plane-v2.json                12,913 B
jarvis/docs/m62/HISTORY_INDEX.md                               10,102 B
jarvis/docs/m62/history/PROGRESS_THROUGH_S3N.md               516,784 B
jarvis/scripts/verify_m62_control_plane.py                     77,631 B
jarvis/tests/test_training_gym_m62_s3n1_control_plane.py       63,883 B
```

### Why these paths

The repository's documentation convention is flat: `jarvis/docs/V69_M<n>_<NAME>.md`, one
file per milestone authority. The S3N.1 milestone document follows it exactly. The archive
and the index do **not**, and that is deliberate — neither is a milestone document, and
dropping a 516 KB event log into the flat namespace beside forty-three milestone
authorities would make the convention harder to read, not easier. They live under
`jarvis/docs/m62/` instead.

The state tree lives at the **repository root**, not inside `jarvis/`. `jarvis/` is the
directory placed on `sys.path` for `training_gym`, `core`, `tools` and `aura`; adding a
`jarvis/state/` directory would put a new importable name next to them. The state tree is
data, it is never imported, and it holds no Python file at all — a test asserts both. It is
also outside the packaged tree, so `MANIFEST.in`'s exclude-first rule keeps it out of every
distribution without a new stanza.

---

## 6 — Trust boundaries

```
                    Git  ──────────────┐   content-addressed, remote-backed
production classes  ─────────────────┐ │   gate/metric/generation digests
frozen milestone docs  ─────────────┐│ │   dataset + candidate identities
                                    ▼▼ ▼
   current.json ──digest──► snapshot ──cross-checked──► VERIFIER ──► PASS / FAIL
                                    ▲
                       archive ─────┘   SHA-256, two witnesses
```

**Inside the boundary** (trusted): Git's object store and refs; the production policy
classes; the milestone documents that sealed each identity; SHA-256.

**Outside the boundary** (verified, never trusted): `current.json`, the snapshot, the
migration manifest, `PROGRESS.md`, the history index, and every sentence any agent has ever
written about M62.

**A JSON file is not authority.** The snapshot is a *claim cache*. Every claim with an
independent source is checked against that source, and the ones without a source — an
adapter's existence, a spent holdout's history — are labelled as sealed milestone records
rather than presented as measurements.

---

## 7 — The archive, and how it was proved exact

```
OLD_PROGRESS_PATH    PROGRESS.md
OLD_PROGRESS_BYTES   516784
OLD_PROGRESS_LINES   6089
OLD_PROGRESS_SHA256  e0914054da4dde4b785bbdabc45a40e0f8b590c2aa3612e9432c685c0c79c1bf
ARCHIVE_PATH         jarvis/docs/m62/history/PROGRESS_THROUGH_S3N.md
ARCHIVE_SHA256       e0914054da4dde4b785bbdabc45a40e0f8b590c2aa3612e9432c685c0c79c1bf
```

Byte identity was established **four independent ways** before a single line of the
original was touched:

1. `cmp --silent` between the source and the archive — identical;
2. `cmp --silent` between an out-of-repository pre-migration copy and the archive —
   identical;
3. SHA-256 equal across all three files, and byte and line counts equal;
4. **`git hash-object` of the archive equals `git rev-parse HEAD:PROGRESS.md`** —
   `279fb9240967158d220ec38eec3675034fc0795e`. Git's own content-addressed object id for
   the committed file equals the object id of the new one, which is the strongest
   statement available: it is the same blob.

A test re-establishes (4) on every run by comparing the archive's bytes against
`git show <subject-commit>:PROGRESS.md`.

**No formatting, newline conversion, header, frontmatter, cleanup, typo fix, whitespace
normalisation or link rewriting was applied.** The archive is now **append-never-edit**. If
its digest fails, the remediation is operator review — never "update the expected hash".

---

## 8 — Migration coverage

Every section of the old document was classified before anything was deleted. The full
table is machine-readable in `state/m62/migrations/0001-control-plane-v2.json`, and a test
asserts that every `## n — ` heading in the archive appears in it with
`orphan_sections: 0`.

| Old § | Classification | New location |
|---|---|---|
| header | CURRENT + SUPERSEDED | `PROGRESS` §1–§2, §12 · snapshot · archive |
| 1 Current checkpoint | CURRENT | `PROGRESS` §1 · `snapshot.project` |
| 2 Status matrix | CURRENT + HISTORICAL + SUPERSEDED | snapshot · `PROGRESS` §2, §4–§7 · index · archive |
| 3 Invariants | FROZEN | `PROGRESS` §8 · `snapshot.frozen_invariants` |
| 4 Timeline + commit index | HISTORICAL + AUTHORITY HISTORY | index · archive |
| 5 Authoritative model | CURRENT + FROZEN | `PROGRESS` §3 · `snapshot.base_model` |
| 6 run-004 smoke | HISTORICAL + FROZEN | index · archive |
| 7 Held-out corpus | CURRENT | `PROGRESS` §5 · `snapshot.datasets` |
| 8 Task pack | FROZEN + POINTER | index · archive · `snapshot.datasets[].pack_hash` |
| 9–12 S3E.2 results, security, performance | HISTORICAL (+ one limitation) | index · archive · `snapshot.limitations` |
| 13 Defect ledger D1–D39 | DEFECT LEDGER | `PROGRESS` §7 · `snapshot.defects` · index · archive |
| 14 Known limitations (94 items) | LIMITATION + SUPERSEDED | `PROGRESS` §7 · `snapshot.limitations` · archive |
| 15 Test baselines | TEST BASELINE | `PROGRESS` §10 · `snapshot.test_baseline` · archive |
| 16 Git checkpoints | AUTHORITY HISTORY | index · archive |
| 17 Runtime artifact policy | FROZEN | `PROGRESS` §8 · verifier path classification |
| 18 What NOT to redo | FROZEN + SUPERSEDED | `PROGRESS` §7, §8, §12 · index · archive |
| 19 NEXT | ACTIVE NEXT + SUPERSEDED | `PROGRESS` §12 · `snapshot.next_milestone` · archive |
| 20 Fast start | CURRENT | `PROGRESS` §11 (rewritten as the bootstrap contract) |
| 21 Update protocol | FROZEN | `PROGRESS` §15 (rewritten for V2) |

```
OLD_SECTIONS_CLASSIFIED:  22
ORPHAN_SECTIONS:          0
```

Thirteen defects still bind operation and became typed state; **D1–D27** are closed
infrastructure history and are routed to their archive section by the index. Sixteen live
limitations were carried forward; the struck-through superseded ones stay in the archive
exactly as written, because a superseded statement is evidence about what was believed
when.

---

## 9 — Canonical serialization

One implementation, in the verifier, used by production and tests alike:

```python
json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
```

UTF-8; keys sorted; two-space indent; NaN and Infinity refused; exactly one trailing
newline. A test asserts `json.dumps` is called in exactly one place in the verifier, so a
second serializer cannot appear quietly.

**The file on disk IS the canonical bytes.** That removes an entire class of bug: there is
no re-serialization step between "the digest" and "the file you can read", and the verifier
asserts `snapshot_bytes == canonical_bytes(parsed)` so a non-canonical file is refused
rather than silently re-hashed.

`ensure_ascii=False` is safe because a separate check refuses non-ASCII in these files —
which is stricter, and catches an accidental smart quote before it becomes an encoding
question.

**No timestamp appears anywhere in `current.json` or a snapshot.** A generation time is
host state; identity must be reproducible by re-derivation. Dates live in this document and
in Git.

---

## 10 — The snapshot chain

```
SNAPSHOT 1  parent = null           ← genesis
     │ sha256 of its canonical bytes
     ▼
SNAPSHOT 2  parent = <that digest>
     │
     ▼
SNAPSHOT 3  parent = <that digest>
```

**No snapshot contains its own digest.** That would not be canonical without special-case
rules, and special-case rules in a hash design are where the bugs live. The digest lives in
`current.json`, and the verifier recomputes it independently.

Enforced: generation starts at 1 · increments by exactly 1 · never duplicates · the
filename's four-digit prefix must equal the payload's generation · the genesis parent is
`null` and nothing else · every later parent equals the SHA-256 of the previous snapshot's
canonical bytes · `current.json` points at the newest.

**Snapshot immutability.** A superseded snapshot is never revised. If one were, its
successor's parent link would no longer match — the chain check catches it.

---

## 11 — Subject commit vs control-plane commit

The self-reference trap is real: a file cannot contain the hash of the commit that
contains it. So the two are separated.

* **`subject_state_commit`** — the commit whose *experimental* state the snapshot
  describes. For generation 1 that is `ec446e34…`, the S3N close.
* **the control-plane commit** — wherever the snapshot itself lands. Not recorded in the
  snapshot, and not needed: Git already knows.

The verifier requires HEAD to be a **descendant** of the subject commit, not equal to it,
and separately requires that no state-bearing production path changed between them.

---

## 12 — Authority separation

```
PROSE_CANNOT_GRANT_AUTHORITY
```

Enforced, not asserted:

* the snapshot carries `authority_observation`, never `authority`;
* **any** key whose name reads as a grant — `authority`, `authorized`, `grant`,
  `capability`, `token`, `may_train`, and sixteen others — is refused anywhere in any
  control-plane document;
* any string value shaped like a spendable plan token is refused;
* the observation is **measured**: the verifier greps every tracked file for a token
  literal on each run, and if that scan cannot run the observation becomes **UNKNOWN and
  the run fails**, rather than defaulting to "none";
* the snapshot must state `control_plane_can_grant_authority: false`, and the schema will
  not accept `true`;
* grant-shaped **prose** is flagged as an ambiguity note — not a failure, because a
  sentence is not a capability, and not silence, because someone writing one is worth
  noticing.

S3N.1 **did not redesign, move, replace or weaken** the existing single-use plan-token
mechanism. TRAIN, EVAL, promotion, registry mutation and release remain governed by it plus
an explicit human decision.

**Demonstrated** (mutation I, §17): a planted `TRAIN authorized: true` in `PROGRESS.md`
raises two ambiguity notes, leaves the observation at `NONE_OBSERVED_IN_REPOSITORY`, leaves
`control_plane_can_grant_authority` false, and creates **zero** spendable tokens.

---

## 13 — Stale-state detection, and what it cannot see

**ENFORCED.** No tracked state-bearing production path may change between
`subject_state_commit` and HEAD without a new state generation. The classified set is
`training_gym/` plus the seven tracked scripts that build corpora, plan training or run
evaluation. Measured this milestone: **0 changed of 0 state-bearing paths**.

**ENFORCED.** `PROGRESS.md` must cite the snapshot path, the snapshot digest, the subject
commit and the archive digest. Human-readable and machine-readable planes cannot drift.

**NOT DETECTABLE, and reported rather than pretended.** A live training or evaluation run
writes an adapter, a generation directory and a ledger — all **gitignored runtime
artefacts**, by the runtime-artifact invariant — plus documentation. Diffing tracked paths
therefore cannot prove that no candidate was trained since the snapshot was written.

```
STALE_STATE_DETECTION: PARTIAL
```

The discipline that closes the gap is procedural, not mechanical: every state-bearing
milestone writes a new generation (`PROGRESS.md` §15). Making it mechanical would mean
hashing runtime artefacts into tracked state, which would put held-out material and host
paths on a path toward a tracked file — a worse trade than the one being made.

---

## 14 — State machines

### Candidates

Derived from the repository, not invented: `CandidateEligibility` supplies the four
post-evaluation verdicts, and the pre-evaluation spellings are the ones `PROGRESS` recorded
for candidates 001–003.

```
NOT_CREATED ──► DESIGNED_UNTRAINED ──► TRAINED_UNEVALUATED ──┬─► EVALUATED_NOT_ELIGIBLE ▪
                                   [TRAIN]                   ├─► EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW ──► PROMOTED ▪
                                                             ├─► EVALUATED_NEEDS_MORE_EVIDENCE
                                                             └─► EVALUATED_QUARANTINED ▪
                                        [EVAL]                        [HUMAN_PROMOTION_AUTHORITY]
```

`▪` terminal. Every pair absent from the table is refused, including
`NOT_CREATED → PROMOTED`, `TRAINED_UNEVALUATED → PROMOTED` and
`EVALUATED_NOT_ELIGIBLE → PROMOTED`. `EVALUATED_NOT_ELIGIBLE` is terminal because a third
candidate is a new identity, never a patch of an old one.

**`PROMOTED` is refused outright in a snapshot.** No promotion mechanism exists in this
repository — `ModelCandidateProposal` is non-effectful by construction — so no artefact
could witness the claim. The state exists in the vocabulary only so that transitions *into*
it can be rejected explicitly rather than by omission.

### Datasets

```
FROZEN_UNUSED ──[EVAL authority consumed]──► USED_IMMUTABLE ▪
```

One edge, one direction. There is deliberately **no** edge back: relabelling a spent
holdout as fresh is the single most damaging edit anyone could make to this state, and it
must be impossible to express, not merely discouraged. An unrecognised or missing status is
a failure, never a fresh corpus.

The S3J/S3K spelling `FROZEN_UNSEEN` means the same thing as `FROZEN_UNUSED`; S3N chose the
latter, and it is the canonical one. The old spelling survives only in the archive and is
recorded in the history index so it is not rediscovered as a third state.

---

## 15 — The verifier

```
python jarvis/scripts/verify_m62_control_plane.py
```

Offline · deterministic · no network · no model · no tokenizer · no train · no eval ·
**no writes**. Read-only `git` plumbing only, with a fixed argv list and `shell=False`.
Thirty checks across thirteen categories:

| Category | Checks |
|---|---|
| SCHEMA | both documents strictly valid; unknown keys refused; the published schema files equal the enforced ones; `jsonschema` run as a second opinion when present and required to agree |
| CURRENT_POINTER | the pointer's digest recomputed from the snapshot bytes; generation, subject commit and schema version agree; both files already canonical |
| SNAPSHOT_CHAIN | genesis semantics, monotonicity, no duplicates, no gaps, parent links, filename/payload agreement |
| ARCHIVE_INTEGRITY | exists; digest and byte count match the snapshot; the migration manifest agrees; both agree with the bytes |
| GIT_AUTHORITY | subject commit exists; HEAD descends from it; branch as declared; `master` as declared, and **failing closed** if no master ref resolves |
| STALE_STATE | state-bearing diff `subject..HEAD`; `PROGRESS`/pointer citation |
| DATASET_STATE | closed vocabulary; the six frozen identities; `eval-v4` `FROZEN_UNUSED` with a declared parent; `eval-v3` `USED_IMMUTABLE`; spent/unspent coherence; transitions |
| CANDIDATE_STATE | closed vocabulary; the three sealed statuses; evidence-shape rules; `PROMOTED` refused; transitions; the NEXT contract |
| POLICY_IDENTITIES | gate, metric and both generation digests **re-derived from the production classes**; `max_new_tokens`; reasoning policy; D37/D38/D39; no D38 gate in `gates.py` |
| AUTHORITY_SEPARATION | observation vocabulary; grant-shaped keys; token-shaped values; a tree-wide token-literal scan; grant-shaped prose flagged |
| HOLDOUT_FIREWALL | no surface names a `v4` task; no body-source reference; no body-shaped key; no over-long string; leak scan; private-path scan; ASCII |
| PATH_INTEGRITY | regular tracked files, no symlinks (files or parent directories), no executable bits, no pointer escaping the repository |
| CONTROL_PLANE_BUDGET | the four size guards; the baseline shape |

Output ends in a machine-readable block naming every category and a problem count.

**Fail-closed everywhere.** An integrity check that *cannot be performed* is a failure: an
unresolvable `master` ref, an uncomputable diff, a failed token scan and unimportable
policy classes each produce a problem rather than a skipped pass.

### Two validators, one contract

The strict validator is written in the standard library, so the control plane still fails
closed on a host without `jsonschema`. The schema files on disk are the **same dict** the
verifier enforces, serialized canonically, and a check refuses any difference — so there is
one authority with a published projection, not two copies that can drift. When
`jsonschema` is importable, both run and **must agree**; a disagreement is itself a
failure. The strict validator also refuses any JSON Schema keyword it does not implement,
so an unenforceable constraint cannot read as satisfied.

---

## 16 — Bootstrap contract and the measured saving

```
LEVEL 0  VERIFY      the control-plane verifier
LEVEL 1  CURRENT     current.json · the snapshot · PROGRESS.md
LEVEL 2  AUTHORITY   the one milestone document NEXT names
LEVEL 3  HISTORY     only the archive section the task needs, via the index
LEVEL 4  ARCHIVE     full read — audit, migration or root-cause only
```

| | Before | After |
|---|---|---|
| Mandatory bootstrap files | 1 | 3 |
| Mandatory bootstrap bytes | **516,784** | **46,552** |
| Reduction | — | **90.99 %** |
| `PROGRESS.md` lines | 6,089 | **510** (**-91.62 %**) |
| `PROGRESS.md` bytes | 516,784 | **27,013** (**-94.77 %**) |

These are **deterministic local byte counts**. They are an
`APPROXIMATE_CONTEXT_REDUCTION_BY_BYTES` and deliberately **not** presented as token
savings: no qualified tokenizer measurement was taken, and the reviewed cache was neither
supplied nor searched for.

### Size guards

Derived from the migrated sizes with headroom — `max(current × 1.5, current + 150)`,
under a reviewed ceiling — and enforced by the verifier.

```
PROGRESS.md        760 lines / 40,960 bytes   (migrated at 510 / 27,013)
snapshot            32,768 bytes              (migrated at 19,141)
current.json         2,048 bytes              (migrated at    398)
history index       32,768 bytes
```

Roughly 250 lines of headroom in `PROGRESS.md` — about a dozen normal milestone closes
before the budget forces a review. Raising a budget is an explicit control-plane migration
decision, never a side effect.

### Concurrency

```
ONE WRITER PER CONTROL-PLANE GENERATION
```

Optimistic concurrency control. Before writing generation *N*, confirm the current
generation is *N−1*; if it is not, **stop**. No last-write-wins, no automatic merge. The
pre-write half is procedure; the outcome is **enforced** — two agents that each produce a
generation 1 leave two files claiming it, and the verifier refuses with
`ONE WRITER PER GENERATION`.

---

## 17 — Tests and non-vacuity

**New file:** `jarvis/tests/test_training_gym_m62_s3n1_control_plane.py` —
**157 tests, 157 passed.** No pre-existing test was changed.

It reads no `eval-v4` task body and contains none: the firewall is tested through the
body-free set identities and the path-level rules S3N declared, and a test asserts the file
itself names no holdout task and no body source.

**Nine bounded deliberate corruptions**, in a throwaway worktree at the subject commit with
the S3N.1 changeset applied and staged. Control before and after: **PASS, 0 problems**.

| | Mutation | Expected | Result |
|---|---|---|---|
| **A** | one byte in the archived `PROGRESS` | archive verification fails | **FAIL**, `ARCHIVE_INTEGRITY`, 3 problems |
| **B** | one byte inside a snapshot string value | pointer digest fails | **FAIL**, `CURRENT_POINTER`, 1 problem |
| **C** | candidate 003 → `TRAINED_UNEVALUATED` | state/evidence verification fails | **FAIL**, `CANDIDATE_STATE`, 2 problems |
| **D** | `eval-v4` `FROZEN_UNUSED` → `USED_IMMUTABLE` | state mismatch fails | **FAIL**, `DATASET_STATE`, 3 problems |
| **E** | gate-policy digest changed | policy identity fails | **FAIL**, `POLICY_IDENTITIES`, 2 problems |
| **F** | unknown JSON key added | schema fails | **FAIL**, `SCHEMA`, 3 problems |
| **G** | snapshot replaced by a symlink | path integrity fails | **FAIL**, `PATH_INTEGRITY`, 1 problem |
| **H** | generation incremented 1 → 3 | chain validation fails | **FAIL**, `SNAPSHOT_CHAIN`, 5 problems |
| **I** | fake `TRAIN authorized: true` in prose | **must create no capability** | **PASS**, 2 ambiguity notes, observation unchanged, 0 tokens created |

Mutation B was authored twice, and the first attempt is recorded rather than quietly
replaced: flipping an arbitrary byte broke JSON *syntax*, so the verifier refused to parse
at all. That is fail-closed behaviour but it demonstrates the wrong thing. The mutation was
re-authored to flip a byte inside a string value, leaving a document that parses perfectly
and is caught purely by its digest — which is the property under test.

The worktree was removed and `git worktree prune` is clean.

### Suites

| Scope | Result |
|---|---|
| New S3N.1 file alone | **157 passed, 0 failed** |
| S3N · D37 · D38 · S3M diagnosis | **240 passed, 0 failed** |
| Adjacent — S3J · S3I.1 lineage · S3F.2 eval-v2 · evaluation corpus · pack builder · task pack · S3G corpus · gates · metrics | **343 passed, 0 failed** |
| **Focused M62 (`-k m62`, `--ignore=tests/test_live_brain_v61.py`, from `jarvis/`)** | **3233 passed, 18 skipped, 0 failed** (3m38s) |

**3233 reconciles exactly: S3N's 3076 + the 157 new tests.** No pre-existing test changed
its outcome.

**The full inner suite was deliberately not re-run.** No shared production infrastructure
changed — the diff is one rewritten document, eight new files and one new test file, and
zero files under `training_gym/`. Existing project policy makes the focused run the
authoritative regression signal for such a tree.

### Static and security gates

| Gate | Result |
|---|---|
| `git diff --check` | **PASS** |
| `compileall` over both new Python files | **PASS** |
| **Ruff** | **NOT RUN — absent from this host**, reported rather than silently skipped |
| **Bandit** | **NOT RUN — absent from this host** |
| Secret scan (`core.redaction_policy.scan_for_leaks`) | **PASS** — three findings, all qualified below, none suppressed or reworded |
| Private-path scan | **PASS** — no new absolute path, username or cache location |
| `TRAIN:` / `EVAL:` token literal scan | **PASS** — none in any tracked file |
| Symlink scan over the changeset | **PASS** |
| Executable-bit scan | **PASS** |
| Tracked runtime-artefact scan | **PASS** — every ignored root empty under `git ls-files` |

The three scanner findings, named rather than quieted:

1. **The archive** reports `reasoning` and `home_path` — and it is a **byte-for-byte copy
   of `HEAD:PROGRESS.md`**, which reports exactly the same two categories. Verified by
   scanning the HEAD blob. **Zero findings added.**
2. **The history index** reports `reasoning` on one line: the literal `<thi`+`nk` inside
   the description of D24, the defect *about* reasoning markup. Operator ruling **H4**
   classifies reasoning markup as hygiene, not a leak.
3. **The verifier** reports `reasoning` on the comment recording that same H4 precedent,
   and `home_path` on the line defining `PRIVATE_PATH_RE` — the regular expression that
   *detects* private paths. Both are the shape S3G, S3J, S3M, S3M.1, S3M.2 and S3N each
   recorded: naming a forbidden token inside the check that forbids it.

Nothing was reworded to satisfy a detector.

---

## 18 — Holdout firewall

**This session read no `eval-v4` task body.** Not `corpus_v4` material, not the promoted
shards, not the materialised task pack. Every `v4` fact used here came from the body-free
sections of `V69_M62_S3N_FRESH_EVAL_V4_FREEZE.md` (§4, §4.1, §4.2, §12, §17), the tracked
lineage constants, and the S3N test file's frozen digests — three independent body-free
sources that agree.

The firewall is enforced **without opening what it protects**, by four structural rules
over the routine bootstrap surfaces:

1. no surface may name an individual `v4` task id — the ids are body-free authority, used
   here purely as a negative test;
2. no surface may reference a body-bearing symbol;
3. no machine-readable surface may cite a body-bearing file as an evidence pointer;
4. no control-plane JSON may carry a body-shaped key or a free-text value over 320
   characters — the D27 lesson, that a body can arrive in instalments.

`PROGRESS.md` deliberately does **not** restate what may and may not be read; it points at
the S3N document §17, which is the authority. That keeps the bootstrap surface small and
lets rule 2 be absolute rather than conditional on a negation.

---

## 19 — Limitations

1. **This is not a defence against a repository rewrite.** An actor who edits the snapshot,
   the archive, the tests and the verifier together defeats every check here. Git is the
   content-addressed history that makes that visible; this architecture is not.
2. **Stale-state detection is PARTIAL** (§13). A run that wrote only gitignored artefacts
   is not detectable by diffing tracked paths.
3. **Some claims have no independent source.** That an adapter exists with a given digest,
   that a holdout was spent by a given milestone — these are sealed milestone records
   cross-checked against the documents that sealed them, not measurements taken here. No
   adapter was hashed and no dataset was rebuilt: both are gitignored runtime artefacts and
   verifying them would need the runtime this milestone had no authority to invoke.
4. **The one-writer rule is enforced post-hoc, not pre-write.** The verifier detects a
   duplicate or skipped generation after it exists; nothing prevents two agents starting.
5. **The task-id firewall rule depends on the S3N ids being reconstructed correctly** from
   the convention recorded in §4.2 of the freeze document. They were not read from the
   corpus.
6. **The context measurement is bytes, not tokens.** No tokenizer measurement was taken.
7. **Ruff and Bandit did not run**; they are absent from this host and nothing was
   installed.
8. **The verifier's own correctness is tested by the verifier's own tests.** The nine
   mutations are the mitigation, and they are bounded: they demonstrate that each check
   fires, not that no check is missing.

---

## 20 — Final status

```
S3N1_CONTROL_PLANE_V2:              PASS
SUBJECT_STATE_COMMIT:               ec446e348995acb0c23a69b0c3efd574f821b1a0

OLD_PROGRESS_BYTES / LINES:         516784 / 6089
OLD_PROGRESS_SHA256:                e0914054da4dde4b785bbdabc45a40e0f8b590c2aa3612e9432c685c0c79c1bf
ARCHIVE_BYTE_IDENTICAL:             YES  (cmp, sha256, byte/line counts, git blob id)
NEW_PROGRESS_BYTES / LINES:         27013 / 510
PROGRESS_BYTE_REDUCTION:            94.77 %
PROGRESS_LINE_REDUCTION:            91.62 %
NORMAL_BOOTSTRAP_FILES / BYTES:     3 / 46552
BOOTSTRAP_REDUCTION:                90.99 %

CONTROL_PLANE_SCHEMA_VERSION:       m62.control_plane.1
STATE_GENERATION:                   1
PARENT_SNAPSHOT:                    null (genesis)
SNAPSHOT_CHAIN:                     PASS
MIGRATION_COVERAGE:                 PASS      ORPHAN_SECTIONS: 0

CONTROL_PLANE_VERIFY:               PASS      PROBLEMS: 0
ARCHIVE_INTEGRITY / STRICT_SCHEMA:  PASS / PASS
GIT_AUTHORITY / STALE_STATE:        PASS / PASS (PARTIAL by design)
CANDIDATE_STATE / DATASET_STATE:    PASS / PASS
POLICY_IDENTITY / AUTHORITY_SEP:    PASS / PASS
HOLDOUT_FIREWALL / PATH_INTEGRITY:  PASS / PASS

PROSE_CAN_GRANT_TRAIN / EVAL:       NO / NO
STATE_JSON_CAN_GRANT_TRAIN / EVAL:  NO / NO
EVAL_V4_BODY_READ_THIS_SESSION:     NO
EVAL_V4_STATUS:                     FROZEN_UNUSED
EVAL_V3_STATUS:                     USED_IMMUTABLE
CANDIDATE_001 / 002:                EVALUATED_NOT_ELIGIBLE (both, unchanged)
CANDIDATE_003:                      NOT_CREATED
D37 / D38 / D39:                    FIXED_UNCHANGED / FIXED_UNCHANGED / OPEN_UNCHANGED
D38_IS_GATE:                        NO

TRAIN_TOKEN_CREATED / CONSUMED:     NO / NO
EVAL_TOKEN_CREATED / CONSUMED:      NO / NO
MODEL_GENERATIONS:                  0
MODEL_RESPONSE_TOKENS_GENERATED:    0
MODEL_WEIGHTS_LOADED:               NO
OPTIMIZER_STEPS:                    0
TRAIN_V3_CREATED:                   NO
CANDIDATE_003_CREATED:              NO

NEW_TESTS:                          157 (157 passed)
NON_VACUITY:                        PASS — 9 bounded mutations, control clean both sides
FOCUSED_M62:                        3233 passed, 18 skipped, 0 failed  (= 3076 + 157)
FULL_INNER_SUITE:                   NOT RE-RUN — no shared infrastructure changed
RUFF / BANDIT:                      NOT RUN — absent from this host
PRODUCTION_SOURCE_CHANGED:          NO
MODEL_PROMOTION / REGISTRY:         NOT_AUTHORIZED / NOT MUTATED
MERGE / TAG / RELEASE / VERSION:    NO / NO / NO / NO
```

---

## 21 — Exact NEXT

**S3N.1 is closed and authorises nothing.** It moved no experiment forward. It built the
surface the next session reads, and the next session is its first consumer.

The next step is unchanged from S3N: a **NEW Claude session** performing **M62
candidate-003 controlled design**, from **body-free `eval-v4` authority only**.

That session must:

1. **verify the control plane first** — `python jarvis/scripts/verify_m62_control_plane.py`
   — and stop if it fails;
2. read `state/m62/current.json`, the snapshot and `PROGRESS.md` — and **not** the
   historical archive;
3. read `V69_M62_S3N_FRESH_EVAL_V4_FREEZE.md` in full (it contains no task body),
   `V69_M62_S3M1_…` and `V69_M62_S3M2_…` as frozen instrument authority, and candidate
   002's training authority;
4. **not** read any `v4` prompt, target, hidden target or task body;
5. change **exactly one** primary axis — training render `MODEL_DEFAULT` → `DISABLED`;
6. keep LoRA scope `ATTENTION_AND_MLP` and candidate 002's configuration otherwise fixed;
7. train on `m62-defensive-quality-train v2` unchanged — **no `train-v3`**;
8. create **no authority**: live training needs a separate `TRAIN` authorisation, and
   evaluation needs a separate single-use `EVAL` authority at a new generation.

Because that session will change candidate state, it is **state-bearing** and must write
**generation 2** on close, following `PROGRESS.md` §15.
