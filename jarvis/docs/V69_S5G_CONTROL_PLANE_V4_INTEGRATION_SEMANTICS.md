# V69 S5G — Control Plane V4: integration authority, declared apart from observed

**GOVERNANCE ONLY. NO SCIENCE MOVED. MASTER WAS NOT TOUCHED.**

0 model loads · 0 generations · 0 authorities created or spent · 0 candidate, dataset,
receipt or policy identities moved. `master` and `origin/master` are
`3705114228edef2f665be349c5c4429b7b16777a` at the start of this milestone and at the end of
it. **No merge, tag, release or version bump was performed, and none is authorised.**

---

## 1 — What was actually broken

S5E made CI reality executable. S5F closed D39. The master-integration dry run that followed
did not fail on Git. Git was healthy, and still is:

```
master is a strict ancestor of the branch
fast-forward available
268 ahead / 0 behind
merge simulation clean
no unique side-branch commits
```

It failed on the control plane. Control Plane V3's `check_git_authority` required

```python
project.master_commit == <the live master ref>
```

and a snapshot committed onto master cannot satisfy that. Committing the declaration is what
moves master past the value the declaration contains.

**This is a self-reference in the schema, not a Git problem.** A commit's SHA is computed
from its content; content that must name the SHA committing it produces is asking for a
SHA-256 fixed point. There is no generation number, no ordering trick and no "just declare
the right value" that reaches it.

### 1.1 — Reproduced, not assumed

Measured on this repository before any V4 code was written, in disposable clones:

| Context | Result |
|---|---|
| generation 33 on the S5F branch | **PASS**, 0 problems |
| the same tree on the S5G branch | **FAIL** — `on branch 'jarvis-v69-s5g-…', the snapshot declares 'jarvis-v69-s5f-…'` |
| local `master` fast-forwarded, `origin/master` old | **FAIL** on the branch half only |
| both `master` and `origin/master` fast-forwarded | **FAIL** on the branch half AND `master_commit` |
| detached HEAD at the integrated commit | **FAIL** on `master_commit`; the branch half **SILENTLY SKIPS** |

And the treadmill itself, three consecutive rounds, each declaring the live tip and then
committing that declaration:

```
declared 3d076a00…  ->  committing created def3f54d…  ->  FAIL
declared def3f54d…  ->  committing created 08f42bd6…  ->  FAIL
declared 08f42bd6…  ->  committing created 0c93b43d…  ->  FAIL
```

The declaration lags the ref it describes by **exactly one commit, permanently**. It does not
converge, and it is not supposed to.

### 1.2 — Two findings the reproduction turned up on its own

**The S5G branch broke V3 the moment it was created.** Generation 33 pins
`project.branch`, so merely branching invalidated the control plane — 7 of 10 666 tests in
the authoritative suite went red for that one reason. That is the fixed point biting in a
second place, and it is why S5G could not be done under V3 semantics.

**V3's branch check was never actually enforced where it mattered.** The guard reads

```python
if code == 0 and branch != "HEAD" and branch != declared_branch:
```

and `git rev-parse --abbrev-ref HEAD` returns the literal string `HEAD` when HEAD is
detached. Measured: the S5G tree, detached at its own tip with master untouched, returns
**exit 0, zero problems**. `actions/checkout@v4` leaves HEAD detached for every
`pull_request` event, so **every PR run in this repository has been skipping that check**.
It was not authority. It only looked like it.

---

## 2 — Models considered, and why three were rejected

Three independent read-only agents mapped V3, designed candidates and attacked them. The
main agent reproduced the load-bearing claims itself rather than accepting them, which
mattered — two of the four candidate models were falsified by measurement.

| Model | Verdict |
|---|---|
| **A** — declared authority state machine with an explicit `mode` field | **Rejected.** A declared `STAGED`/`INTEGRATED` mode has to be flipped *at* the moment of merge, so it needs a new generation to become true. That contradicts the requirement that one immutable record be valid both before and after integration. |
| **B** — governance carrier commit (`git log -1 -- state/m62/current.json`, target must BE the carrier) | **Rejected, empirically.** Generation 33's seal is `272a8d4`; the branch tip is `3d076a0`, **two commits later** (a test fix and a docs commit). A carrier-equals-tip rule files a false FAIL against the repository as it actually stands. Scoped down to state-bearing paths it collapses into `check_stale_state`, which already does that job. |
| **C** — bare subject ancestry (`subject` is an ancestor of the target) | **Rejected, empirically.** It accepts an unrelated master advance, and it accepts arbitrary commits appended after the governed subject. |
| **D** — A+C synthesis: frozen anchors, derived relationship enum | **Adopted as the base, but incomplete.** See §3. |

### 2.1 — Why bare ancestry had to be strengthened

Model D was the recommended design. Measured against real Git repositories, it accepts two
things it must not:

```
honest integration (master ff'd to the real tip)      D: PASS   V4: PASS
+ one commit touching jarvis/core/                    D: PASS   V4: UNGOVERNED_TRAILING_COMMIT
+ one commit touching jarvis/scripts/train_experiment.py
                                                      D: PASS   V4: UNGOVERNED_STATE_ADVANCE
```

The second row is an unauthorised runtime change on master. The third is a **tampered
training entrypoint** — a state-bearing production path. Both are contained by "the target
contains the governed subject", and both are exactly what an integration authority exists to
refuse. So V4 is Model D plus a **governance closure** on what rode along.

---

## 3 — What V4 declares, and what it derives

### 3.1 — The declaration is immutable and names only the past

```json
"integration_authority": {
  "integration_base":  "3705114228edef2f665be349c5c4429b7b16777a",
  "governed_subject":  "<a commit that already existed when this record was written>",
  "target_ref":        "refs/heads/master",
  "method":            "FAST_FORWARD_ONLY"
}
```

Every field is a commit that already exists or a constant from a closed enum. **No field can
name the commit that carries the record**, which is the property V3 lacked. It is checked
directly rather than asserted: the migration tool refuses to emit unless every referenced SHA
already resolves, and a test refuses a declaration naming a commit that does not exist.

### 3.2 — The observation is derived on every run and never written down

```
TARGET_AT_AUTHORIZED_BASE         target == integration_base            ADMITTED
INTEGRATED_FAST_FORWARD           governed descendant, nothing ungoverned rode along   ADMITTED
TARGET_ADVANCED_WITHOUT_SUBJECT   target moved, but not along this lineage
TARGET_REWRITTEN                  target does not descend from the base: rollback, force-move, unrelated history
UNGOVERNED_STATE_ADVANCE          a state-bearing production path changed past the subject
UNGOVERNED_TRAILING_COMMIT        a path outside the governed trailing surface changed past the subject
TARGET_UNRESOLVABLE              the ref does not resolve — FAILS CLOSED, never admitted
```

Two are authority. Five are failures. `TARGET_UNRESOLVABLE` is deliberately not admitted:
"the ref was not available" is not evidence that the target is where the authorisation
permits.

### 3.3 — What replaced `merged_into_master`

Nothing declares it any more. `merged_into_master: false` was a frozen boolean that **no
check ever verified against Git** — it appears in the V3 verifier only as a schema constant.
It was false before the push and would have gone on reading false after it. Under V4 the
question "has this been integrated?" is answered by `observe_integration_state` from live
refs, so it cannot be stale and cannot lie.

### 3.4 — What replaced branch-name authority

Ancestry. `project.branch` survives as **provenance** and is explicitly not a gate; a test
asserts that changing it does not change the verdict. Lineage is established by requiring
HEAD to descend from `governed_subject`, which is identical attached and detached and cannot
be bypassed by how the tree was checked out.

This is a **net strengthening**, not a relaxation. V3's branch gate was absent in every
detached context. V4's ancestry check is present in all of them, and V4 adds a target-side
closure V3 had no equivalent of.

---

## 4 — The same record, three contexts

The load-bearing property, asserted on the byte-identical declaration:

| Context | Observation | Verdict |
|---|---|---|
| S5G branch, master at the base | `TARGET_AT_AUTHORIZED_BASE` | PASS |
| master fast-forwarded to the governed tip | `INTEGRATED_FAST_FORWARD` | PASS |
| detached HEAD at the same commit | `INTEGRATED_FAST_FORWARD` | PASS |

No record is rewritten between them. A test freezes the declaration to JSON before observing
and re-checks it afterwards, so "observing does not mutate the declaration" is measured
rather than assumed.

---

## 5 — Fail-closed matrix

Every case below is built as a real throwaway Git repository, not a mock.

| # | Case | Result |
|---|---|---|
| 1 | wrong integration base (subject does not descend from it) | declaration refused as incoherent |
| 2 | governed subject absent from the target's history | `TARGET_ADVANCED_WITHOUT_SUBJECT` |
| 3 | target on unrelated history (non-fast-forward) | `TARGET_REWRITTEN` |
| 4 | target rolled back behind the base | `TARGET_REWRITTEN` |
| 5 | unauthorised target ref | refused; `_resolve_target` returns nothing for any name but the hardcoded one |
| 6 | unauthorised method | refused |
| 7 | target ref missing entirely | `TARGET_UNRESOLVABLE`, not admitted |
| 8 | unauthorised runtime commit after the subject | `UNGOVERNED_TRAILING_COMMIT` |
| 9 | tampered state-bearing path after the subject | `UNGOVERNED_STATE_ADVANCE` |
| 10 | record copied onto an unrelated lineage | refused — HEAD does not descend from the subject |
| 11 | declaration naming a commit that does not exist | refused |
| 12 | V3 snapshot read under V4 rules | impossible — dispatch is on `schema_version` |
| 13 | V4 state read under V3 rules | impossible — same dispatch |
| 14 | emptied record store under V4 | refused (see §7) |
| 15 | detached HEAD | same verdict as attached, asserted |
| 16 | ungoverned runtime path while the target is STILL STAGED | refused (see §5.1) |

### 5.1 — The closure ran on the wrong lineage, and it was measured

The first implementation put the whole governance closure inside `_observe_one`, which
compares the governed subject to the **target**. That is the wrong lineage to rely on alone.
While the target still sits at the authorised base — the staged case, which is *every run
made before anyone integrates, including the run an operator reads to decide whether to* —
`_observe_one` returns `TARGET_AT_AUTHORIZED_BASE` on its first comparison and the
trailing-path scan never executes at all.

Measured on the real tree, not argued: a commit adding `jarvis/core/new_runtime_engine.py`
between the governed subject and HEAD verified **PASS, 0 problems**, and became visible only
once master had already been moved onto it. `check_stale_state` did not cover it either — it
denies the hardcoded `STATE_BEARING_PRODUCTION` list and nothing else, so every path nobody
had thought to enumerate passed by default. That is exactly the "unknown, therefore harmless"
default a deny-by-default closure exists to remove, surviving in the half of the design
nobody had pointed a test at.

The fix is step **2b** of `check_integration_authority`: the same closure, applied to
`subject..HEAD` — the lineage that actually accumulates commits. `closure_offenders` is now a
single function called by both sites, because a closure that exists twice is a closure that
can be weakened once, and a test asserts neither caller re-implements the membership test.
An uncomputable `subject..HEAD` diff fails closed as UNKNOWN rather than clean.

Eleven trailing paths were then measured in the staged context. Permitted: `jarvis/docs/`,
`jarvis/tests/`, `state/m62/`, `PROGRESS.md`. Refused: a new `jarvis/core/` module, a new
top-level module, a new unclassified `jarvis/scripts/` script, the training entrypoint,
`pyproject.toml`, `.github/workflows/ci.yml`, `requirements/base.txt`.

---

## 6 — The future successor, which is what makes this architecture

A migration that only makes one merge possible is a patch. The test that matters is whether
generation N+1 can exist without another treadmill.

It can, and it is proved in an isolated repository:

1. the target is fast-forwarded — generation N reads `INTEGRATED_FAST_FORWARD`;
2. a future product commit lands, ungoverned — generation N now reads
   `UNGOVERNED_TRAILING_COMMIT` and **fails**, which is the point;
3. a successor declares `integration_base` = the previously-integrated tip and
   `governed_subject` = the new commit. **Both already exist when it is written.**
4. before its own integration the successor reads `TARGET_AT_AUTHORIZED_BASE`; after it,
   `INTEGRATED_FAST_FORWARD`.

No self-SHA, no fixed-point search, no rewriting of generation N. Generation N's bytes never
change; only the derived observation does.

---

## 7 — What V3 keeps, and one bug this migration exposed

V2 and V3 generations are read under V2/V3 semantics, unchanged. `check_git_authority`
dispatches to the V4 path in its first two lines and everything below that guard is exactly
the check the first 33 generations were sealed under. Generation 33 still hashes to
`8b8b0f05c6ad34f4256c5a9d33276d5aa87717cbd66f3534276d736a8ca51cfa`, and the V3 schema still
requires `master_commit` and `merged_into_master`.

**One real bug was found by an existing test while migrating.** `check_record_store` was
gated `if not cp.is_v3: return`. V4 uses the same content-addressed store, so a V4 generation
would have skipped record-store integrity entirely — an emptied store would have been
accepted in silence. The S4B test that exists precisely to catch a silently-skipped check
caught it. It is now gated on "content-addressed", not on "V3".

A second, related defect was introduced and fixed during the same work: the V4 semantic shape
and the V4 container share a `schema_version`, unlike V3 (which rehydrates to V2's version
string), so a rehydrated document looked like a container and was rehydrated twice. The
discriminator is now the presence of the `records` map. Both are recorded here rather than
quietly fixed, because a migration that only reports its successes is not evidence.

---

## 8 — Limitations, stated plainly

- **A hostile commit confined to `jarvis/docs/` or `jarvis/tests/` rides along.** The
  trailing surface must permit docs and tests, because generation 33 genuinely has two such
  commits after its seal and a stricter rule would fail the repository as it stands. The CI
  suite is the control for that surface, not the control plane. This is the one gap in §5.
- **The verifier itself is inside the permitted trailing surface.** `VERIFIER_PATH` has to
  be, since changing it is what a control-plane milestone does. It is not an additional
  exposure — the verifier that runs is whatever is at HEAD regardless of what any allowlist
  says — but it does mean the closure cannot be the control for a weakened verifier. The
  suite and review are.
- **An exact rollback to the declared base is invisible.** If the target is integrated and
  then force-reset to exactly `integration_base`, the observation reads
  `TARGET_AT_AUTHORIZED_BASE` — indistinguishable from "never integrated". No point-in-time
  check of a single ref can see history no ref retains. V3 had the identical blind spot.
  The control is branch protection, not verifier plumbing.
- **`tagged` and `released` are still frozen booleans.** They are still false and nothing in
  S5G moves them, but they carry the same shape `merged_into_master` did. The day a milestone
  needs to record a real tag, it should follow §3.2, not add a boolean.
- **The observation trusts the refs it is given.** It is evidence about this checkout, not an
  attestation about the remote.

---

## 9 — What this milestone does NOT claim

It does **not** claim master was merged, and the recorded observation says so:
`TARGET_AT_AUTHORIZED_BASE`. It does not create authority of any kind, move any scientific
record, or make candidate 006, `eval-v8`, training, promotion, a tag or a release any more
permitted than they were. `PROSE_CANNOT_GRANT_AUTHORITY` applies to this document.

What it does claim is narrower and checkable: **the control plane can now represent the
integration it was always going to have to represent**, and the next attempt can be argued
against V4 semantics instead of against a fixed point.
