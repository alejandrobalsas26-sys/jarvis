# V69 S5E — Repository Reality Reconciliation

**Branch:** `jarvis-v69-s5e-reality-gate`
**Source:** `1f48f5bdf7bb1118501d3ac243b1d18a8a5cb709` (M65C seal)
**Master:** `3705114228edef2f665be349c5c4429b7b16777a` — unchanged, not merged
**Scope:** repository reality audit. No feature work. No scientific state moved.

S5E is not a feature milestone. It asks one question of the repository:

> Do the claims this repository makes about itself resolve to commands, imports,
> release facts and Git topology that actually exist?

For eight things, the answer was no. This document records what was measured, how, and
what changed. Six of the seven were invisible to every run done from a developer's
working directory; the seventh was invisible until the tree was cloned afresh.

---

## 1 — The headline

**At the M65C seal the workflow's own authoritative job exited 2 and ran zero tests.**

```
$ cd <repo>                                    # the repository root, as ci.yml requires
$ python -m pytest -q --tb=short jarvis/tests tests
...
E   ModuleNotFoundError: No module named 'tests.support'
!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!
10580 tests collected, 2 errors in 25.99s
EXIT 2
```

Measured on CI's declared interpreter — CPython **3.11.16**, pytest **8.4.2** resolved
through `requirements/constraints-ci.txt`, `dev` + `soc` profiles installed, exactly as
`.github/workflows/ci.yml` specifies. Not inferred from a developer host: a standalone
3.11 was fetched precisely so this would be CI's interpreter and not a proxy for it.

Collection was **interrupted**, which is the important word. The gate did not run 10,580
tests and fail two. It collected 10,580 tests, discarded all of them, and ran none.

Everything else in this document was found underneath that.

---

## 2 — Defect 1: the authoritative test entrypoint did not resolve

### The mechanism

Not a path accident. A package shadow:

```
<repo>/tests/__init__.py          exists      -> a REGULAR package
<repo>/jarvis/tests/              no marker   -> only a PEP 420 namespace portion
```

CPython's `PathFinder` accumulates namespace portions as it scans `sys.path` but returns
the first spec that has a real loader. **A regular package therefore wins regardless of
`sys.path` order.** Verified directly, with `<repo>/jarvis` at `sys.path[0]` and `<repo>`
at `[1]`: `tests` still resolved to `<repo>/tests/__init__.py`.

| working directory | `tests` resolves to | `tests.support` |
|---|---|---|
| `<repo>` (CI) | `<repo>/tests/__init__.py`, `SourceFileLoader` | **ModuleNotFoundError** |
| `<repo>/jarvis` | namespace portion `<repo>/jarvis/tests` | resolves |

The milestone suites were run from `jarvis/`. CI runs from the repository root. Same
tree, opposite result, decided entirely by the working directory.

### Why the obvious fixes are wrong

Both were tried and measured, not argued:

| candidate | result |
|---|---|
| add `jarvis/tests/__init__.py` | 2 errors → **22**. Breaks every bare sibling import and collides head-on: two regular `tests` packages, five shared basenames. |
| delete `<repo>/tests/__init__.py` | 2 errors → **7**. Still no `tests.support`, plus new `import file mismatch` — that marker is what keeps `test_cisco_controller`, `test_critic`, `test_grc_auditor`, `test_pcap_capture` and `test_security` apart across the two trees. |
| `jarvis.tests.support` | requires `jarvis/__init__.py`, which does not exist — the layout is flat (`packages = ["core","tools","aura"]`). |

### The fix

`jarvis/tests/support/` → `jarvis/tests/_test_support/`, imported as a repo-unique top
level rather than through the ambiguous `tests.` prefix. Uniqueness is what makes the
import independent of `sys.path` order, and therefore of the working directory.

It works through the same mechanism the suite already relies on for bare sibling imports
like `from test_mesh_live_turn_v69_m64_1 import LiveTurn`: `jarvis/tests` has no
`__init__.py`, so pytest's `prepend` mode puts it on `sys.path` under either cwd. That
import is on line 39 of the same file whose line 49 was failing — it succeeded in the
very traceback that recorded the failure, which is what proved the mechanism sound before
anything was moved.

**It stays under `jarvis/tests/` deliberately.** `MANIFEST.in` prunes `tests`, and
`check_package_manifest.py` rejects any built artifact containing a `/tests/` fragment. A
sibling directory next to `core/` would have escaped both and shipped the crash harness in
the wheel. Directory depth is preserved too, so the harness's
`WORKER.parent.parent.parent` still resolves to the runtime root.

---

## 3 — Defect 2: the same bug, thirteen more times

Fixing the import raised collection from 10,580 + 2 errors to 10,626, and the full run
then produced **25 failures**. Thirteen were the identical root cause wearing different
clothes — tests reading tracked sources through bare cwd-relative paths:

```
FileNotFoundError: 'core/specialist_team.py'
FileNotFoundError: 'training_gym/training/backends/transformers_peft.py'
```

Those paths exist only when the working directory is `jarvis/`. Anchored to an explicit
`_APP_ROOT` derived from `__file__`, so the CWD cannot decide the answer.

The full 25, so none is left unaccounted for: **13** CWD-relative source reads (this
section), **2** interpreter/profile failures (§4), **3** stale Bandit facts (§6) and
**7** control-plane `GIT_AUTHORITY` failures — the last self-inflicted, because creating
the S5E branch is exactly what generation 32 exists to declare.

**The Control Plane had already recorded this and closed it.** Generation 31 carries:

```json
"known_invocation_artifact": {
  "failing_tests": 8,
  "file": "jarvis/tests/test_training_gym_m62_s3g2_validation_wiring.py",
  "invocation": "pytest run from the repository root instead of jarvis/",
  "is_a_regression": false,
  "note": "... those tests read production source by a path relative to the working
           directory, so they resolve only from jarvis/. ... Not a rider fix."
}
```

The diagnosis was exactly right. The classification was not: this is not an artifact of
an alternative invocation. **It is the authoritative invocation.** The governance layer
had measured the symptom, labelled it benign, and moved on — which is the single most
instructive thing S5E found, and the reason generation 32 records it as resolved rather
than quietly dropping the field.

---

## 4 — Defect 3: two failures only CI's interpreter and profile could show

* `Path.read_text(newline=...)` is **Python 3.13+**. CI runs 3.11, where it is a
  `TypeError`; the developer host runs 3.14, where it is not. The kwarg was passed
  `None` — already the default — so it was simply dropped. This is the one finding that
  could not have been made without fetching a real 3.11.
* `aura.server` imports **FastAPI**, which is in `requirements/all.txt` and *not* in the
  `dev`+`soc` profile CI installs. A hard import turned an optional dependency into an
  unconditional CI failure; it is now `importorskip`, matching how the repo treats every
  other optional dependency.

---

## 5 — Defect 4: the lint gate was red too

`ruff check .` from `jarvis/` — CI's Gate 2, mandatory — reported **27 errors, exit 1**.

Not version drift: ruff 0.16.5 (the version in the developer's own `.venv`) and 0.16.6
(what `constraints-ci.txt` resolves today) report the identical 27. Note that the
constraint file's own comment warns that "a ruff minor bump adds new rules that turn a
green tree red" and then pins only `>=0.4.0,<1.0.0`; that looseness is not what caused
this, but it remains an open exposure.

All 27 were dead code — 20 `F401`, 3 `F402`, 3 `F541`, 1 `F841` — and all were fixed
rather than ignored. No new per-file ignore, no widened `select`, no `noqa` blanket. Two
judgement calls: the `F841` kept its call and dropped only the dead binding (the effect
committing is the point of the test), and the `F402` loop variables in
`verify_m62_control_plane.py` were renamed rather than suppressed, found via AST because
ruff reports one shadow per scope: fixing the reported 3 surfaced 2 more, so 5 loops were
renamed in total against 3 reported findings.

---

## 6 — Defect 5: the Bandit Low ceiling had been breached for five weeks

**The blocking gate was never weakened and never in question.**
`bandit -r core tools -ll -q` → **0 Medium, 0 High, exit 0**, command unchanged.

Below it, `BANDIT_LOW_BASELINE = 488` against a real count of **489**.

| | |
|---|---|
| Set | M61.8 closure commit — re-measured at that commit, still exactly 488. It was correct. |
| Breached | `436119b` "feat(runtime): add situational world-state fabric", 2026-08-29 |
| Noticed | never, for five weeks |

The reason nothing failed is the whole point of this milestone. The ceiling's only
enforcement is `tests/test_bandit_low_baseline_v69_m618.py`, which runs in CI's
authoritative job — **and that job could not collect.** One defect masked the other.
(`scripts/check_bandit_low_baseline.py` is invoked by no workflow step at all; it is a
developer tool, and its docstring now says so.)

Both new findings are **accepted, not fixed, and not suppressed** — adding a `nosec`
would have grown `BANDIT_SUPPRESSION_COUNT`, which is itself pinned and regression-tested:

| finding | disposition |
|---|---|
| `core/runtime_doctor.py` B105 "possible hardcoded password" on `PASS = "pass"` | A `DoctorStatus` enum member. A status string, not a credential. Renaming an enum to satisfy a substring heuristic would be worse code. |
| `core/world_connectors.py` B110 `try/except/pass` around `writer.wait_closed()` in a `finally` | Swallowing a failure while closing an already-failed connection is correct; raising there would replace the real error with a teardown error. |

Ceiling deliberately re-approved at **489**, with this triage, and every document that
quotes it refreshed. `BANDIT_SCANNED_LINES` had drifted 75,519 → 88,908 — 13,389 outside
its own 5% tolerance — and is now derived.

---

## 7 — Defect 6: facts that could not detect their own falsehood

The consistency checker verifies that release *documents* quote the *constants*. It does
not verify that either matches the repository. Measured by mutating a clone and
re-running `scripts/check_release_consistency.py`:

| mutation | verdict |
|---|---|
| `DETERMINISTIC_TESTS_PASSED` 4128 → 5000, facts only | **CAUGHT** |
| …and update every document to match | **MISSED — PASS** |
| `RELEASE_MERGE_COMMIT` → a commit that does not exist, facts + documents | **MISSED — PASS** |
| `BANDIT_LOW_BASELINE` 488 → 100000 (the ceiling silently removed) | **MISSED — PASS** |
| inject a module with `exec()`, `shell=True`, `md5` — Bandit then reports 2 High | **MISSED — PASS** |
| delete `docs/releases/v69.61.0.md` entirely | **MISSED — PASS** |

The last is a structural hole and is now closed: `_release_texts` skipped a declared
document that was absent, so deleting the release notes did not fail the checker — it
**emptied the `counts` and `security` families**, which then reported no problems over no
documents. A missing declared document is now a problem.

Two claims had nothing behind them, and are corrected:

* `release_facts.py` credited `test_release_closure_v69_m618` with re-measuring the
  deterministic counts "with `--collect-only` arithmetic". That module contains no such
  code and never did.
* "`RELEASE_MERGE_COMMIT` … cross-checked against git" was prose. The only consumer
  asserted the value appears in the release documents — the same declaration on both
  sides of the comparison. It is now actually asked of git (it exists, and it is an
  ancestor of master).

And one tautology: `test_the_declared_observation_matches_reality_or_is_conservative`
asserted `OBSERVED >= current or current <= BASELINE`, whose right disjunct is the ceiling
assertion immediately above it — already passed, so the `or` could never be false. The
test named "matches reality" checked nothing of the sort.

### What was *not* stale

`DETERMINISTIC_TESTS_PASSED = 4128` looks alarming next to a 10,626-test tree, and an
audit flagged it as drift. It is not. It describes the **v69.61.0 release commit** —
master is M61.8, and the unreleased M62–M65C work on top of it adds several thousand
tests. The constants are now explicitly scoped so the next reader does not have to
re-derive that.

---

## 7b — Defect 7: the local tree is not the checkout CI gets

The local authoritative run was green. `git clone` + the same command + the same
interpreter was **EXIT 1**: 5 failures and 5 errors. Everything that differed was
gitignored runtime evidence a developer host happens to have and `actions/checkout`
never produces. This was found by running the clean clone BEFORE pushing, and it is the
reason that step is not optional.

* **Nine tests** in `test_training_gym_m62_s4f_sealed_state.py` read
  `evaluation/protocol-v4-attempts.jsonl` and the per-generation results
  unconditionally. That evidence quotes held-out material, so the root `.gitignore`
  refuses to commit it and `MANIFEST.in` refuses to package it — correctly, and that is
  not going to change. So the tests must report "not present" rather than fail.

  These are **skips, not relaxations**: where the evidence exists all nine run unchanged
  and can still fail (verified — 125 passed, **zero skipped**, in the developer tree).
  The sixteen tests in that file which read TRACKED evidence are deliberately left
  unguarded, so a bare checkout still proves the sealed verdict, the spent holdout, that
  005 is not eligible, that 004 is still held and that nothing was promoted.

* **`test_the_runtime_adapter_is_not_tracked`** asserted
  `git check-ignore -q jarvis/training_runs`. The rule is `training_runs/` —
  directory-only — so git can only match the bare path when it can see the path IS a
  directory, which means when it exists on disk. A fresh checkout has never created the
  runtime tree, so the probe returned 1 and the assertion failed. It now probes a path
  *inside* the tree: checkout-independent, and the stronger claim anyway.

This is the same failure mode as the import shadow one layer out. That gate passed
because of the **working directory**; this one passed because of the **checkout**.
Neither was visible to any local run, and both were red in CI.

## 7c — Defect 8: CI checks out one commit, and the suite needs history

Found by a red team AFTER the milestone believed itself finished, and it is the sharpest
instance of S5E's own thesis turned back on S5E.

`.github/workflows/ci.yml` uses `actions/checkout@v4` eight times and set `fetch-depth`
nowhere. The default is **1**. Measured:

```
$ git clone --depth 1 <repo> -b jarvis-v69-s5e-reality-gate shallow
$ cd shallow
  commits available : 1
  master ref        : ABSENT
  origin/master     : ABSENT
  git cat-file -e 0bb1a6b^{commit} : fatal: Not a valid object name
$ python -m pytest -q jarvis/tests tests   ->  26 failed          EXIT 1
```

This suite verifies the M62 control plane, which resolves `subject_state_commit`,
requires HEAD to descend from it, and compares `master` against a declared invariant.
`check_git_authority` **fails closed** when master does not resolve — correctly, since
"the ref was not available" is not evidence that master is untouched. On a depth-1
checkout that is unconditional.

**One of the 26 was a test S5E itself added.**
`test_the_declared_merge_commit_exists_in_this_repository` runs `git cat-file` on
`RELEASE_MERGE_COMMIT`, and a shallow clone does not have that object. In making a
circular claim executable, S5E introduced a new CI-only failure of precisely the class it
exists to remove. That is worth stating plainly rather than quietly fixing.

Both halves are addressed:

* the authoritative job now checks out with `fetch-depth: 0`, and **only** that job —
  the other seven touch no git, and the compat job's four suites were verified to pass
  unchanged on a depth-1 clone;
* the two git-dependent tests skip on a shallow checkout, because asserting against
  history that was never fetched is a finding about the checkout, not the repository;
* `test_the_authoritative_job_checks_out_full_history` pins the workflow setting, so the
  cheap checkout cannot come back silently.

Three layers of the same defect, in one milestone: the import shadow depended on the
**working directory**, the sealed-state tests on the **checkout's contents**, and this on
the **checkout's depth**. Each was invisible locally, and each made the blocking job red.

---

## 7d — What the red team could still falsify

A fresh read-only red team was given the finished branch and asked to break it. It found
defect 8 above, and four things this document had overstated. All are fixed; they are
recorded because a milestone about honest claims should not hide the claims it got wrong.

| finding | disposition |
|---|---|
| The "generator can NEVER touch the ceiling" guard was a substring check on one dict literal — bypassed by registering the key after it | Now asserted on the runtime object AND behaviourally, against a text whose ceiling is absurd (on the real tree approved == measured, so a rewrite would be invisible) |
| Gutting `rewrite()` to `return text, []` left every generator guard green | New test feeds a known-stale text and requires the drift to be reported |
| `test_the_low_ceiling_cannot_be_removed_without_the_scan_noticing` claimed a control it does not implement | Renamed to `..._is_still_wired_to_a_real_scan`, with the limit stated in the docstring |
| `pytest.importorskip("fastapi")` turned a red HUD **security allowlist** test into one that never ran in CI at all | Reverted properly: the allowlist is now read by parsing `aura/server.py`, so the assertion runs on every profile and additionally pins that the constants stay auditable literals |
| Doc §10 claimed no ref held unique commits, measured before S5E created its own backup branch | Branch deleted, sweep re-run, caveat recorded in §10 |
| Ruff breakdown summed to 29, not 27 | Corrected in §5 |

Two further criticisms are accepted and NOT closed, because closing them would be worse:

* **Nine sealed-state tests skip in CI** (§7b). A red team is right that a single-spend
  control on `eval-v7` is among them. It is not unguarded: `test_eval_v7_is_spent_and_names_its_spender`
  reads the **tracked** control plane and runs everywhere, so the spend is still asserted
  in CI — what skips is the cross-check against the gitignored ledger.
* **D39 remains open, and the green run is order-dependent.** Measured:
  `pytest …dataset_exports.py …s3g2_validation_wiring.py` → 117 passed;
  reversed → **4 failed**. Alphabetical collection puts the exports file first, so the
  authoritative invocation does not trigger it. That is luck, not a fix. S5E replaced
  PROGRESS §10's old "4 failed" row with a measured "0 failed"; the row was about a
  different invocation, but the ordering caveat belongs in the record and is now in §7
  and §10 of PROGRESS.

---

## 8 — What is measured now

| gate | command | result |
|---|---|---|
| consistency | `python scripts/check_release_consistency.py` | PASS |
| lint | `ruff check .` · `compileall` | PASS |
| **authoritative suite** | `python -m pytest -q --tb=short jarvis/tests tests` **from the repository root** | **exit 0** |
| **the same, in a fresh `git clone`** | identical command and interpreter | **exit 0** — 10,576 passed, 84 skipped |
| stabilization | `python scripts/soak_stabilization_m61.py --json` | PASS |
| doctor | `python scripts/doctor.py` | PASS |
| compat (3.12) | consistency + compileall + 4 suites | PASS |
| base-install | clean venv, `base.txt` only, optional packages proven absent | PASS |
| packaging | wheel + sdist + manifest/secret scan | PASS |
| security-scan | `bandit -r core tools -ll -q` | PASS (0 Medium, 0 High) |
| dependency-audit (advisory) | `pip-audit -r requirements/base.txt` | no known vulnerabilities |

Every one on CI's declared interpreter. `compat` on a real 3.12. `base-install` in a
genuinely clean environment, not a developer venv.

## 9 — What now defends this

Two test files that **execute** rather than read. `test_ci_workflow_v69_m612.py` asserts a
great deal about the workflow — gates present, no swallowed exit code, Bandit's label
matching its threshold — and every one of those assertions reads the workflow as *text*.
That is how a milestone sealed green while its authoritative job exited 2.

* `tests/test_ci_entrypoint_reality_v69_s5e.py` — runs the workflow's own pytest command,
  extracted from `ci.yml` rather than hardcoded, and requires it to collect; resolves the
  support package in a **fresh interpreter** from both working directories and compares
  `__file__`; and asserts **the shadow still exists** (the repo-level marker present,
  `jarvis/tests` without one, the basenames still colliding) so the guard cannot rot into
  a tautology once someone "tidies up".
* `tests/test_release_fact_reality_v69_s5e.py` — asks git about the merge commit; requires
  the measured facts to have a generator, that generator to be a fixpoint, and the facts
  to be current; and asserts the generator's derived set does **not** contain the approved
  ceiling — checked over the source, because a behavioural test would pass on today's tree
  where measured and approved happen to be equal.

`scripts/regenerate_release_facts.py` separates **measured** facts (regenerated) from the
**approved** ceiling (never touched by a generator — raising a security baseline stays a
reviewed decision).

Deliberately not asserted anywhere: that CI passed remotely. A test cannot know that, and
pretending otherwise is the class of circular fact this milestone exists to remove.

---

## 10 — Git topology and integration

Measured, not assumed:

| | |
|---|---|
| master is an ancestor of the branch | **YES** (`merge-base --is-ancestor`, exit 0) |
| merge base | `3705114…` — master itself |
| ahead / behind master | **254 / 0** before S5E |
| merge commits in `master..HEAD` | **0** — all 254 single-parent, strictly linear |
| branches or tags with unique commits | **none**, across all 31 refs |
| merge simulation | `git merge-tree --write-tree` → result tree **identical** to HEAD's tree: a pure fast-forward, 0 conflicts |

The ten milestone branches (M62 → M65C) are bookmarks at increasing offsets on one trunk,
not parallel lines of development. Every merge commit in the repository's history predates
master; the post-master convention is a linear chain. Two `refs/claude/checkpoint-*` refs
carry one WIP commit each, both superseded, both with parents already in the chain.

**A caveat on when that was measured.** The sweep above ran before S5E made its own
commits, and S5E then created a `s5e-backup-before-amend` branch while reorganising them
— which stranded four commits behind a claim of "none", for exactly as long as nobody
re-measured. A red team caught it. The branch has been deleted and the sweep re-run:
across every `refs/heads`, `refs/remotes` and `refs/tags`, zero refs hold a commit that
HEAD does not. The lesson is the milestone's own: a measurement is about the moment it
was taken, and the audit has to be repeated after the auditor edits the thing.

Nothing is stranded and nothing needs recovering before integration.

---

## 11 — Scientific state: unchanged

S5E has no authority over scientific history and exercised none.

| | |
|---|---|
| candidate 004 | `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` / HOLD |
| candidate 005 | `EVALUATED_NOT_ELIGIBLE` / HOLD_FOR_RESEARCH |
| eval-v7 | `USED_IMMUTABLE`, still spent exactly once |
| candidate 006 · eval-v8 | absent |
| training runs | none |

`state/` is byte-identical to the source commit apart from the new generation-32
snapshot and its pointer. Both verdicts above were **re-derived** from body-free evidence
by the control-plane verifier, not read from a claim.

---

## 12 — What S5E did NOT do

No merge. No tag. No release. No deployment. No model promotion. No training. No
`candidate006`, no `eval-v8`. Master is untouched. Generation 31 is not modified — where
it recorded something S5E later disproved, the correction lives in generation 32, so the
history shows what was believed then and what was measured later.

---

## 13 — Before a human authorises a merge

1. Remote CI must be green on this branch — local parity is strong evidence, not proof.
2. Decide whether the loose `ruff>=0.4.0,<1.0.0` pin should be tightened; it can turn a
   green tree red with no source change, which is the exposure the file's own comment
   warns about.
3. Decide the integration shape. A fast-forward is available and conflict-free; a
   `--no-ff` merge commit matches the repository's own historical convention and keeps
   the milestone boundary visible in the graph.

The remaining risk is not in the Git graph — that part is clean and measured. It is
semantic: whether 254 commits of unreleased work behave correctly in production. S5E
raises the confidence that the gates measuring that work now actually run; it does not
substitute for the judgement of merging them.
