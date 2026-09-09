# V69 S5G.1 — pre-integration hardening: master-state semantics and two trust boundaries

**GOVERNANCE ONLY. NO SCIENCE MOVED. MASTER WAS NOT TOUCHED.**

0 model loads · 0 generations of a candidate · 0 authorities created or spent · 0 candidate,
dataset, receipt or policy identities moved. `master` and `origin/master` are
`3705114228edef2f665be349c5c4429b7b16777a` at the start of this milestone and at the end of
it. **No merge, tag, release or version bump was performed, and none is authorised.**

This milestone exists because an independent pre-master reconciliation of S5G found three
reproducible code-side findings and one external one. **Every one of them was reproduced on
the S5G source before a line was changed**, and none is repaired here on the strength of a
report alone.

**Two further red teams then falsified this document's own drafts.** The first found D52
and D53; the second, run against the fix for those, found D54. All three are §5. They are
recorded rather than quietly folded in, because the failure they demonstrate is the one this
milestone is about: *a control that reads closed and is not.* **Read a closure claim here as
measured only where a reproducer is cited.**

---

## 1 — The four findings, as measured

### F1 — a legitimate integration turned the authoritative suite red · REPRODUCED

Three tests in the authoritative suite pinned the *not-yet-integrated* observation as a
permanent invariant:

| Test | File |
|---|---|
| `test_master_is_unchanged_and_nothing_was_merged_tagged_or_released` | `…m62_s4h_s4f_result_invariance.py` |
| `test_the_project_block_did_not_move_master_or_merge` | `…m63_s4b_control_plane.py` |
| `test_the_project_block_did_not_move_master` | `…m63_s4c_trained_state.py` |

Each asserted `V.observe_integration_state(authority)[0] == "TARGET_AT_AUTHORIZED_BASE"`.

Reproduced in a disposable clone of `11f2343`, CI-parity CPython 3.11.16. **The commit was
byte-identical in both runs; only the ref moved.**

```
master, origin/master at 3705114 (staged)      3 passed
master, origin/master at 11f2343 (a legal FF)  3 failed
    assert 'INTEGRATED_FAST_FORWARD' == 'TARGET_AT_AUTHORIZED_BASE'
```

`ci.yml` triggers on `push: branches: [master]`. So the **first** CI run after an authorised
integration was deterministically red, and the only way to make it green again was to roll
`master` back to the base — the exact-base rewrite this programme forbids and that GitHub
does not currently prevent (F4). S5G rescoped these three away from the frozen
`merged_into_master` boolean, which was right, and pinned the wrong half.

### F2 — the checker certified its own weakening · REPRODUCED

`VERIFIER_PATH` was on `INTEGRATION_TRAILING_PATHS`. A trailing commit could therefore edit
the checker, and the edited checker was then the thing that certified the lineage that had
edited it. Reproduced with **one deleted line** and one added file, in a single commit:

```
$ sed -i '6164d' jarvis/scripts/verify_m62_control_plane.py     # check_git_authority(cp, report)
$ cat > jarvis/core/s5g1_repro_runtime_engine.py                # ungoverned runtime
$ git commit -am "..."
$ python jarvis/scripts/verify_m62_control_plane.py --quiet
M62_CONTROL_PLANE_VERIFY: PASS
GIT_AUTHORITY: PASS          <- never executed
INTEGRATION_AUTHORITY: PASS  <- never executed
PROBLEMS: 0
$ python -m pytest -q --tb=line jarvis/tests tests
10662 passed, 83 skipped                                        exit 0
```

Two independent defects made that possible, and both are closed here:

1. the checker was inside its own allowance, so the edit was **governed**;
2. `Report.status` returned `PASS` for any category with no recorded problem, so **a check
   that never ran was indistinguishable from a check that ran and found nothing**.

The second is why the block printed `PASS` twice for a function nothing called. S5G assigned
the control for a hostile trailing commit to "the CI suite"; the suite passed too.

### F3 — the authoritative suite could erase its own failures · REPRODUCED

`tests/` was on the trailing surface, and the authoritative command names **both** trees, so
a `pytest_collection_modifyitems` hook in the repo-level tree receives every `jarvis/tests`
item as well. Reproduced with the exact CI invocation, on a tree whose only real failures
were F1's:

```
real failing condition          3 failed, 10660 passed, 82 skipped     exit 1
+ trailing tests/conftest.py    0 failed, 10660 passed, 3 deselected   exit 0
control plane on that lineage   PASS, PROBLEMS: 0
```

The conftest was an ordinary, plausible, *governed* file with a governance-shaped comment.
Running only `jarvis/tests` never loads it, so the effect is invisible unless the exact CI
invocation is reproduced — which is why it survived a milestone explicitly about closure
defects.

### F4 — external master protection · MISSING

Read-only, and **not fixed here**:

```
GET /repos/{owner}/jarvis/rulesets                []
GET /repos/{owner}/jarvis/branches/master         {"protected": false}
GET .../branches/master/protection                404 Branch not protected
```

Recorded as **`EXTERNAL_CONTROL_PENDING`**. An exact-base rollback is **DETECTED** by the V4
observation and **PREVENTED by nothing**.

### D52, D53 — found by a second red team, against this document's own first draft

Two further BLOCKERs, both reproduced by the author afterwards. See **§5**.

---

## 2 — The rule

> **The governed subject is the only place executable authority may change. After it, a
> lineage may carry inert governance evidence and nothing else.**

That is the whole model. Everything below is its mechanics.

### 2.1 The closure gained a second dimension

Being on the permitted surface is necessary and **no longer sufficient** — the file must also
be inert. `jarvis/docs/conftest.py` and `state/m62/sitecustomize.py` are on the surface and
are code, and "unknown extension, therefore harmless" is the same mistake as "unknown path,
therefore harmless", one level down.

```python
INTEGRATION_TRAILING_PATHS = (STATE_DIR + "/", PROGRESS_PATH, "jarvis/docs/")
TRAILING_INERT_SUFFIXES = (".md", ".json")
```

`VERIFIER_PATH`, `jarvis/tests/` and `tests/` were **removed**, and those removals are the
milestone. The suffix allowlist is **derived, not guessed**: every tracked file under
`state/m62/` is `.json` and every tracked file under `jarvis/docs/` is `.md` — **measured
against the live tree by a test**, not counted here. A literal count in prose is stale the
first time a milestone closes, and this document's first draft carried one that its own
governance commit invalidated.

`closure_offenders` now returns three classes — `state_bearing`, `executable`, `ungoverned` —
because "this moves the science", "this is code on the evidence surface" and "nobody has ever
classified this" are different findings. `UNGOVERNED_EXECUTABLE_TRAILING` joins the observed
states and is **not** admitted.

### 2.2 Silence stopped being success

`CHECK_DISPATCH` replaces twenty-two bare call statements with data: each entry names a check
and the categories it **owns**. `Report.claim` records that a check executed; `Report.status`
returns **`NOT_RUN`** for a category nothing claimed, and `Report.ok` is false while any
category is unclaimed. Deleting a dispatch entry now costs the attacker the `PASS` it used to
buy for free.

### 2.3 `check_verifier_integrity`, sharing no dispatch with the closure

A new category, `VERIFIER_INTEGRITY`, asking three questions that do **not** route through
`check_git_authority` — because the S5G attack deleted exactly that call, and a second control
that dies with the first is not a second control:

1. **Structural.** Does `CHECK_DISPATCH` name every `check_*` this module defines (bar the
   internally dispatched `check_integration_authority`), and does exactly one entry own each
   reported category?
2. **Byte identity.** The live generation declares `governed_implementation.verifier_sha256`,
   and the checker on disk must hash to it. Required from **generation 35 onward**;
   generations 1–34 predate it and are never retrofitted.
3. **Lineage.** No `AUTHORITY_CRITICAL_PATHS` entry — the checker, both test trees,
   `conftest.py` and `jarvis/conftest.py` *by exact path*, `.github/workflows/`, and every
   pytest/packaging configuration file — may differ between the governed subject and `HEAD`.
   A `conftest.py` at an unlisted location (`mcp_servers/`, `jarvis/core/`) is caught by the
   closure's deny-by-default alone; this second control does not reach it.
4. **Target coherence.** The integration target's *own* `current.json` must parse and name a
   snapshot the target carries, read with `git show` out of the target tree. Path
   classification says which files moved; it cannot say whether what they now contain is a
   control plane at all (D54).

**No self-SHA and no fixed point.** The generation names the checker's digest; the checker
never names its own. The ordering is *finalise the checker → commit it as the subject →
digest it → write the generation*, which is the discipline `integration_authority` already
follows, and a test asserts the digest does not appear in the checker's own source.

---

## 3 — What this is not

`check_verifier_integrity` is **in** the checker. Anyone who can rewrite the checker can
rewrite its self-audit, and no arrangement of code inside one file changes that. What S5G.1
removes is the **silent** version: after this milestone a lineage that touches the checker,
the suite, collection configuration, a workflow or packaging is **not a governed lineage at
all**. It requires a new governed subject and a successor generation — a commit a human reads
and the hash chain seals.

The residual is external, and it is the same one §5 names.

---

## 4 — Governance closure, classified

| Surface | After S5G.1 |
|---|---|
| Runtime code (`jarvis/core/`, new top-level modules) | **DENIED** trailing |
| Training / evaluation (`STATE_BEARING_PRODUCTION`) | **DENIED** trailing, and separately by `check_stale_state` |
| The checker | **SUCCESSOR-GOVERNED** — closure, digest pin and lineage check |
| Authoritative tests (`jarvis/tests/`, `tests/`) | **SUCCESSOR-GOVERNED** |
| `conftest.py` / pytest plugins / `pyproject.toml` / `pytest.ini` / `tox.ini` / `setup.cfg` | **SUCCESSOR-GOVERNED** |
| CI workflow (`.github/workflows/`) | **SUCCESSOR-GOVERNED** |
| Pure evidence: `state/m62/**.json`, `PROGRESS.md`, `jarvis/docs/**.md` | **PERMITTED** trailing — see the append-only rule below |
| A sealed artefact under `state/m62/` or `jarvis/docs/m62/history/` | **APPEND-ONLY *past the governed subject*** — may be added, never modified or deleted; `current.json` is the sole exception (D53). Inside a subject it is reviewable, not refused |
| The control-plane verifier's own CI job | **MANDATORY**, run as a step and not as a test (D52) |
| Any file pytest reads as configuration — `conftest.py`, `pyproject.toml`, `pytest.ini`, `tox.ini`, `setup.cfg` | **ALLOWLISTED IN CI** to exactly three, plus a check that the control-plane modules survive collection (D54) |
| Anything else, any other suffix | **DENY BY DEFAULT** |

A documentation-only trailing change stays permitted because it is *provably* non-executable
by the suffix allowlist — not because documentation is assumed harmless.

**The close topology changed with it.** Until S5G.1 a governance commit could carry the
checker. It cannot now, so a milestone closes as: *subject commit carrying code, tests, the
checker and the published schemas* → *governance commit carrying `state/m62/`, `PROGRESS.md`
and `jarvis/docs/` only*. This milestone's own two commits are that shape, which is the
cheapest available proof that the tightening did not produce a deadlock.

---

## 5 — What the red teams found in this document's own drafts

Three rounds of findings, every one reproduced by the author afterwards, every one
against a claim §2, §4 or §6 had already made. D52 and D53 came from a red team run against
the first draft; D54 from a second red team run against the fix for those.

### D52 — the checker was never in CI

§6 item 3 said the closure "rejects such a lineage BEFORE pytest can become the authority".
There was no such sequencing, because `.github/workflows/ci.yml` **did not run the verifier
at all**:

```
$ git grep -l verify_m62 .github/
(nothing)
```

It reached CI only through a handful of pytest tests — and the authoritative command names
both trees, so a trailing `tests/conftest.py` deselects exactly those tests. Measured on the
first draft: verifier **FAIL** on a tree carrying an ungoverned runtime module, and
`pytest -q --tb=short jarvis/tests tests` **exit 0** on that same tree.

**Fixed** by a mandatory `control-plane` job running
`python jarvis/scripts/verify_m62_control_plane.py` as its own step with `fetch-depth: 0`. A
collection hook can deselect a test; it cannot deselect a job.

### D53 — the digest pin's reference value was editable beside it

§2.3 presented `governed_implementation.verifier_sha256` as byte identity. It is one line of
JSON **inside the live snapshot**, and the live snapshot is a `.json` on the permitted
trailing surface. One trailing commit — weaken the checker, reseal that line, add the
ungoverned module and the masking conftest — reproduced:

```
M62_CONTROL_PLANE_VERIFY: PASS
GIT_AUTHORITY: PASS   INTEGRATION_AUTHORITY: PASS   VERIFIER_INTEGRITY: PASS
PROBLEMS: 0
```

The same class of edit rewrote an evaluation receipt's measured delta, gutted
`scientific-suite.json` from 54 modules to 12, and rewrote the append-never-edit archive while
`ARCHIVE_INTEGRITY` still read PASS — because the record carrying the expected digest was
rewritten beside it.

**Fixed** by `SEALED_TRAILING_ROOTS`: everything under `state/m62/` and
`jarvis/docs/m62/history/` is **append-only** past the governed subject. A trailing commit may
ADD a snapshot or a record and may move `current.json`; it may never modify or delete one.
Enforced from `git diff --name-status`, because `--name-only` cannot tell an addition from a
rewrite and that blindness was the whole of D53. Both lineages check it, and
`PROGRESS_REQUIRED_CLAUSES` separately stops a trailing commit deleting the standing
`EXTERNAL_CONTROL_PENDING` conclusion.

### D54 — the masking route needed no conftest, and the target's state was never read

Found by a second red team, against the fix above.

**(a)** The conceded §3 residual also empties `AUTHORITY_CRITICAL_PATHS`, which is the only
thing naming `pyproject.toml`. So the *same one commit* could add a tracked **root**
`pyproject.toml` whose `[tool.pytest.ini_options] addopts` `--ignore`d the two control-plane
modules. It shadows `jarvis/pyproject.toml` as the configfile for the authoritative
invocation. Measured on the branch:

```
authoritative collection   10,830 tests  ->  10,659 tests
control-plane tests                171  ->        0
pytest -q --tb=short jarvis/tests tests              exit 0
the conftest-only shell guard                        "allowlisted only", exit 0
```

**Fixed** by widening the guard to every file pytest reads as configuration, allowlisted to
exactly `jarvis/conftest.py`, `jarvis/tests/conftest.py`, `jarvis/pyproject.toml` — and by a
second step that runs the **authoritative** collection and refuses fewer than 150
control-plane tests. It must use the authoritative argument list: `--ignore` does not apply
to a path named explicitly, so naming the two modules collected 171 on exactly the tree the
check exists to catch.

**(b)** `sealed_rewrites` exempts `current.json` by construction, and an *added* `.json`
under `state/m62/` is inert and governed. So a target carrying `{"pointer": "forged"}`, or
one that simply deleted `PROGRESS.md`, was observed as **"a governed fast-forward"** and
admitted. **Fixed** by `_target_state_incoherence`, which reads the target's own pointer with
`git show` and returns the new non-admitted observation `TARGET_STATE_INCOHERENT`.

**(c)** The shape guard was satisfiable by moving the needles into a **docstring**, and
evaded entirely by `@pytest.mark.skip` — neither of which the comment-stripping version saw.
**Fixed** by parsing the function and unparsing only its statements, and by refusing a test
carrying a disabling decorator.

**(d)** The guard's `surfaces=$(git ls-files …)` was **fail-open**: command substitution does
not trip `set -e`, so outside a repository it reported "only the allowlisted ones" having
enumerated nothing. **Fixed** by redirecting to a file, which makes `git` the command whose
status `set -e` acts on.

Also recorded, not fixed, because they are honest limits rather than defects: append-only
applies *past* the subject and not inside it; and the two-commit route (weaken in the
subject, seal in a successor) is legitimate by design — but it used to end with the verifier
affirming *"checker pinned"* and *"no authority-critical path moved"* while saying nothing
about the checker having changed. It now emits **`THE CHECKER CHANGED between generation N-1
and N`** as a note, so the reviewer the design relies on is told.

### What the red teams could NOT break

F1 held completely — all four invalid target states still fail, widening
`ADMITTED_INTEGRATION_STATES` fails all four tests, no other test pins a pre-integration
observation, and the full suite is green with master fast-forwarded. Deny-by-default held for
`sitecustomize.py`, `.pth` files, a root `conftest.py`, `jarvis/tests/__init__.py`,
`pyproject.toml` `addopts`, the workflow and `jarvis/docs/plugin.py`. V4 sustainability held,
with no fixed point.

**The honest residual it named, and this milestone keeps:** inside a governed subject, a
`.json` or `.md` that a gate READS is still authority as data. The closure makes such a change
reviewable — it forces a new subject and a successor generation — it does not make it
self-proving. Human review of the subject is the control, and that is stated rather than
engineered around.

---

## 6 — What remains, and what must happen before master moves

**`MASTER_BRANCH_PROTECTION: STILL_REQUIRED.`** S5G.1 closes the code boundary. It does not
give `master` an anti-rewrite rule, and the repository has none: 0 rulesets,
`"protected": false`. An exact-base rollback — restoring `master` to
`3705114228edef2f665be349c5c4429b7b16777a` after an integration — is detected by the
observation on the next run and prevented by nothing in between.

Before `master` may move:

1. legitimate integration must stay green — **done, and proved in both contexts**;
2. the checker must not certify its own weakening — **closed** for a trailing lineage, with
   the residual in §3 and the D53 reseal route closed in §5;
3. the suite must not be able to erase its failures while inheriting old authority —
   **closed**, and since D52 the verifier is a CI job of its own rather than a test;
4. future V4 successors must remain constructible — **proved, no self-reference**;
5. **the repository must externally prevent an exact-base rewrite — NOT DONE.**

Only then may master integration be retried, and it is a separate human decision.
