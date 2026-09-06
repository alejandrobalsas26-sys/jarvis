# V69 S5F — D39: order-independence of the export test surface

**Scope.** One defect. No feature work, no model work, no autonomy work. D39 was the last
thing S5E left deliberately open: the same relevant test surface had different outcomes
depending on collection order.

    dataset_exports  THEN  s3g2_validation_wiring   ->  117 passed
    s3g2_validation_wiring  THEN  dataset_exports   ->    4 failed

The authoritative suite happens to collect them in the passing order. That is not
isolation, and S5F closes it at the root rather than at the ordering.

**Status: D39 is FIXED.** `RESOLVED` is not a value the control plane can hold —
`DEFECT_STATES` in `verify_m62_control_plane.py` is a closed enum — so the ledger records
`FIXED`, which is also the accurate word: the cause was removed, not masked.

---

## 1 — Reproduction

Measured on CI's declared interpreter: CPython **3.11.16**, pytest **8.4.2**, `dev` + `soc`
profiles resolved through `requirements/constraints-ci.txt`, exactly as
`.github/workflows/ci.yml` specifies. Run from the **repository root**, which S5E
established as the authoritative working directory.

```
$ python -m pytest -q jarvis/tests/test_training_gym_m62_s3g2_validation_wiring.py \
                      jarvis/tests/test_training_gym_m62_dataset_exports.py
4 failed, 112 passed, 1 skipped
```

117 nodes either way. The skip is environment-dependent and has nothing to do with D39
(see §6); on a developer tree carrying the gitignored quality corpus the same run reads
`4 failed, 113 passed`. The reverse order is green, and each file alone is green (47 and 70). The four failures are
exactly the historical four:

```
test_every_record_being_revoked_is_reported_honestly
test_an_altered_export_manifest_is_detected
test_an_export_is_never_overwritten
test_a_surviving_export_manifest_alone_still_blocks_a_re_export
```

They are also, exactly, the **only four `pytest.raises(ExportError, ...)` sites in the
file** — lines 191, 240, 248 and 258. That correspondence is the whole diagnosis.

## 2 — The record was wrong about the mechanism

The S3M-era note recorded D39 as failing "on a shared export root (`corpus/v1` already
exists)". That reading is incorrect, and it is worth stating plainly because it sent the
first hour of this milestone looking at the filesystem.

There is no shared export root. Every path in both files is a per-test `tmp_path` or a
`tmp_path_factory.mktemp`. Running the contaminating file leaves `git status --porcelain`
byte-identical, and the victim file passes in a fresh process immediately afterwards.

What the note mistook for on-disk residue was **the victim test's own expected error text**,
printed as an uncaught exception. The message

```
ExportError: export: sft_train.jsonl already exists for corpus/v1; an export is as
immutable as the version it came from
```

is precisely what `test_an_export_is_never_overwritten` asks the exporter to say. The test
did not fail because the exporter misbehaved. It failed because `pytest.raises(ExportError)`
did not catch it.

The historical documents are not rewritten. This section is the correction, in current
truth.

## 3 — Root cause

`jarvis/tests/test_training_gym_m62_s3g2_validation_wiring.py`, in
`test_the_validation_export_authority_imports_no_framework`, asserted that importing the
export authority pulls in no ML framework by doing this:

```python
importlib.reload(importlib.import_module("training_gym.datasets.export"))
```

`ExportError` is **defined in that module**, at `jarvis/training_gym/datasets/export.py:120`.
`importlib.reload` re-executes the module body in the module's existing `__dict__`. The
module object survives; every class it defines does not. Measured:

```
same module object after reload:                True
ExportError is the same class:                  False
issubclass(new_ExportError, old_ExportError):   False
stale export_sft.__globals__ IS the module dict: True
stale export_sft sees the NEW ExportError:      True
```

The last two lines are the mechanism. pytest imports every selected module before running
any test, so `test_training_gym_m62_dataset_exports.py` had already bound `ExportError`
**by value**. After the reload its `export_sft` — resolved through the shared module dict —
raises the **new** class, while its `pytest.raises(ExportError)` still names the **old**
one. They are unrelated classes, so the exception propagates and the test fails carrying
the exact message it was waiting for.

`__module__` and `__qualname__` are identical across the reload, which is why the traceback
looks completely normal. That is what hid this.

**Why only those four.** The file's other refusal tests catch `SchemaError` (lines 141, 146,
174, 181) or `PreferenceError` (lines 377–573), whose defining modules were never reloaded.
The new `ExportError` still subclasses the unchanged `SchemaError`, so those keep catching
normally. Only the four sites naming `ExportError` itself were exposed.

**Why the order mattered.** Alphabetically `..._dataset_exports.py` sorts before
`..._s3g2_validation_wiring.py`, so the authoritative collection ran all four victims before
the reload ever happened. The green result was a property of filename sort order.

### Classification

| Question | Answer |
|---|---|
| Class | `IMPORT_ORDER_DEPENDENCY`, via `GLOBAL_MUTATION_LEAK` of a module namespace |
| State channel | The class object `training_gym.datasets.export.ExportError` — not a path, file, env var or cache |
| Survives a process boundary | **No.** B in one process, A in another: 47 passed |
| Filesystem residue | **None.** `git status` byte-identical before and after |
| Product bug or test bug | **TEST.** `importlib.reload` appears nowhere in production |

`importlib.reload` does not occur in `jarvis/core`, `jarvis/training_gym`, `jarvis/tools`,
`jarvis/scripts` or `jarvis/aura`. The nearest neighbours — `start_hot_reload`,
`config_manager.reload()` (re-reads a config dict) and `plugin_loader._PluginWatcher._reload`
(calls `load_all()`, a manifest re-read) — never rebind a module's classes. A real user
running two exports in one process goes through the same, never-reloaded module. The
product's export lifecycle (`export.py:453–466`, refuse-if-exists on **both** the data file
and the manifest) is correct, and is in fact what the victim tests were asserting.

## 4 — Minimal reproducer

One contaminating node, **sufficient and necessary**:

```
jarvis/tests/test_training_gym_m62_s3g2_validation_wiring.py::test_the_validation_export_authority_imports_no_framework
```

* that node + the victim file alone: the same 4 failures
* that node deselected, then the full reversed pair: 116 passed, fully green
* the node + any single victim: 1 failed

The victim set is not a group. Each of the four fails alone with the single contaminator.

## 5 — The repair

`importlib.reload` is replaced by a **subprocess probe**. This is not an invention; it is
the pattern V69 M61 RC1 already established for exactly this hazard, in
`test_managed_path_migration_v69_m61_rc1.py:82` and `test_managed_logging_v69_m614.py:74`,
whose docstrings say it outright:

> `importlib.reload` on a shared `core` module rebinds its classes, so a later test catching
> `managed_paths.UnsafeLeafName` would compare against a different class object and fail for
> a reason unrelated to the code under test.

The export authority was the one place still doing it. The repair applies the house rule.

**The old assertion was also vacuous.** It sampled `sys.modules` before and after the
reload, inside a process where the rest of the suite had already imported whatever it
imports — and a reload cannot un-import `torch`. `after == before` therefore held no matter
what `export.py` pulled in. A virgin interpreter actually tests the claim. The repair
strengthens the test it repairs.

State lifetime, before and after:

| | Intended lifetime | Actual lifetime before | After |
|---|---|---|---|
| `training_gym.datasets.export` namespace | process-global, immutable | mutated mid-suite by a test | process-global, immutable |
| the import-purity measurement | one virgin interpreter | leaked into the shared process | its own subprocess |

Nothing in production changed. No fixture was added, no scope reduced, no cache cleared.

## 6 — What was explicitly NOT done

No collection reordering. No `xfail`, no `skip`, no retry, no sleep. No autouse
reset-everything fixture, no `sys.modules` hammer, no module reloading between tests, no
per-test subprocess isolation, no file renaming, no change to the CI test order. **0 skips
and 0 xfails were added.** The one pre-existing skip on this surface
(`the promoted quality corpus is not present in this checkout`, a gitignored runtime
artefact) is untouched and is why a clean clone reports 116 passed + 1 skipped where a
developer tree reports 117 passed.

## 7 — The sentinel

`jarvis/tests/test_order_isolation_d39_v69_s5f.py`, three nodes, two independent guards.

1. **Behavioural** — both known orders must exit 0 in fresh interpreters, parametrized so
   each order is its own reported node. It asserts on the **return code**, never on stdout:
   a sentinel that greps for "passed" is satisfied by a run that collected nothing.
2. **Structural** — the root cause as an invariant: no test may `importlib.reload` a module
   whose classes another test module (or itself) binds at import time. That is the precise
   hazard *condition*; what the guard can **see** is a matter of coverage, and §8 states it
   exactly rather than claiming the defect can never return.

   It scans **both** trees CI collects (`jarvis/tests` and the repo-root `tests`),
   recursively, including `conftest.py` and helper packages. It recognises
   `importlib.reload` and a bare `reload` from `from importlib import reload`, and as the
   target: a literal `import_module("x.y")` (attribute or bare), a bound alias, a dotted
   `a.b.c`, and `sys.modules["x.y"]`.

The structural guard deliberately permits safe reloads. `test_training_gym_m62_evaluation_runner.py`
reloads `training_gym.evaluation.backends` while `EvaluationBackendError` is defined in
`training_gym.evaluation.backend` — a **different** module — so no identity churn reaches it.
Verified by running that reload immediately before a `pytest.raises(EvaluationBackendError)`
test: 2 passed. The remaining reloads
(`evaluate_adapter_cli.py:278`, `dataset_bridge_cli.py:297`, `train_experiment_cli.py:79`,
`test_plugin_exec_v69_m617.py:323`) target modules that define no class any test binds by
name; consumers use module-attribute access, which a reload keeps correct.

### Non-vacuity

The sentinel was run against the historical source `e27e14e8`, unmodified:

```
FAILED ...::test_both_known_orders_pass_in_a_fresh_process[wiring_then_exports]
FAILED ...::test_no_test_reloads_a_module_whose_classes_are_bound_elsewhere
2 failed, 1 passed
```

Both guards fail on the defect, independently. `[exports_then_wiring]` passes there, which
is correct — that order was always green, and a sentinel that failed it would be measuring
something else.

## 8 — Known limitations

* The structural guard is a static reader, not a dataflow analysis. A reload whose target
  is assembled at run time from a computed string is out of reach. The six literal forms it
  does cover are enumerated in §7 and each is regression-tested in §11; the first version of
  this guard saw only two of them, which the red team demonstrated by reintroducing live D39
  four different ways.
* A module-scope alias (`ExportError = export.ExportError`) is not a `from`-import and is
  not counted as a holder, and relative `from .export import ...` holders are skipped.
  Neither form occurs in either test tree today.
* It inspects **test** modules as holders. `training_gym/datasets/__init__.py:168`
  re-exports `ExportError` by value, so any future reload would also desynchronize the
  package facade from the module. Nothing reloads it today; this is a hazard, not a bug,
  and the structural guard forbids the reload that would activate it.
* The behavioural guard costs two extra pytest processes (~5 s total). It is scoped to the
  two implicated files, not to a shuffle of the full suite.

## 9 — The order matrix

The identical harness was run against the historical source `e27e14e8` and against the
S5F tree. Every sequence runs in a fresh interpreter unless it says "same process".

| Dimension | `e27e14e8` | S5F |
|---|---|---|
| A -> B, x5 | 5/5 pass | 5/5 pass |
| B -> A, x5 | **0/5** | 5/5 pass |
| same process, both orders | **1/2** | 2/2 pass |
| ABA, same process | **0/1** | 1/1 pass |
| BAB, same process | **0/1** | 1/1 pass |
| A -> B -> A, three processes | 3/3 pass | 3/3 pass |
| B -> A -> B, three processes | 3/3 pass | 3/3 pass |
| node permutations, all 120 of the 5-node reproducer | **24/120** | **120/120** |
| randomized whole-surface orders, 20 fixed seeds, 117 nodes | **5/20** | 20/20 pass |
| `PYTHONHASHSEED` 0/1/7/42/12345 | **0/5** | 5/5 pass |

Two rows carry most of the meaning.

**24/120.** Of the 120 permutations of the four victims plus the one contaminator, exactly
the 4! = 24 that place the contaminator **last** were green. Nothing survives it and
everything before it is untouched — which is what a class rebind looks like, and what
filesystem residue does not.

**A -> B -> A across three processes passed even on the broken source.** The contamination
does not cross a process boundary. That is the measurement that rules out the filesystem
theory the historical note recorded, and it is why a fresh-process-only fix would have
proved nothing here. The same-process rows are the real claim, and they are green.

## 10 — Targeted mutation campaign

Sixteen mutations. The generic list (cache invalidation, registry reset, environment
restore, session-scoped fixtures, shared temp paths) is **not applicable** to this root
cause — no cache, registry, env var, fixture or path is involved — so the campaign
attacks what is actually here rather than performing ceremony.

**A/B — reintroduce or weaken the repair. All 8 detected.**

| # | Mutation | Result |
|---|---|---|
| A1 | restore the historical in-process reload | detected |
| A2 | reload via module alias (`import x.y as m`) | detected |
| A3 | reload the export module from a NEW test file | detected |
| A4 | reload `training_gym.schemas` instead | detected |
| A5 | reload `training_gym.datasets.manifests` | detected |
| A6 | reload `training_gym.datasets.candidate` | detected |
| B1 | probe imports a module that does not exist | detected |
| B2 | probe reports a banned framework | detected |

A4–A6 matter: the guard is not pinned to one module name. B1 and B2 prove the repaired
test's two assertions — exit status and empty stdout — each earn their place.

**C/D — is each sentinel assertion load-bearing?** With the defect present and *both*
guards intact, weakening either one is still caught by the other (C1–C4), which is the
redundancy the two-guard design is for. Deleting the partner first isolates each
assertion, and then all four weakenings let the defect escape:

| # | Weakening, with the partner guard deleted | Defect escapes? |
|---|---|---|
| D1 | behavioural guard drops the reverse order | yes — load-bearing |
| D2 | behavioural guard greps stdout instead of the exit code | yes — load-bearing |
| D3 | structural guard loses the alias form | yes — load-bearing |
| D4 | structural guard loses the literal `import_module` form | yes — load-bearing |

D2 is the one worth keeping in mind: `"passed" in stdout` is satisfied by
`4 failed, 112 passed`, so a sentinel written that way would have reported D39 as closed.

## 11 — Red team

An independent read-only agent was given the closure and told to falsify it. It ran roughly
284 ordered pytest invocations, re-derived the 24/120 and 120/120 permutation results
independently, and confirmed that pytest honours command-line node order — so the matrix in
§9 measures what it claims.

**Blockers: none.** All six masking forms were answered negative with evidence: no state
reset, no reloading, no order forcing, no hidden skip or xfail (skip/xfail marker count
across `jarvis/tests` is **118 before and 118 after**), no suppressed failure, no autouse
fixture, and no change under `jarvis/core`, `jarvis/training_gym`, `jarvis/tools` or
`jarvis/aura`. The victim file is untouched and all four `pytest.raises(ExportError,
match=...)` sites survive with their patterns intact.

It confirmed independently that the repaired test cannot silently no-op — an unimportable
target, a bad `sys.path` or a crashed subprocess all land on the returncode assert — that
the remaining reloads are safe (`training_gym/evaluation/backends/__init__.py` defines
**zero** classes; the CLI and `plugin_loader` holders use module-attribute access, which a
reload keeps correct), and that the control-plane edits are corrections rather than
relaxations: the verifier's check is a symmetric inequality, so writing `OPEN` over a frozen
`FIXED` is exactly as strong as the reverse.

### The one MAJOR, and what was done about it

The structural guard's **stated scope was false.** This document and the sentinel docstring
claimed the defect "cannot return through a different module"; the guard actually recognised
two call shapes. The red team reintroduced **live D39** — the same four failures — through
four literal, idiomatic, non-computed forms it could not see.

The wording was not softened to match the guard. The guard was rewritten to match the claim,
and every form is now a regression test. Each was reintroduced in a clean clone and the
guard confirmed to fire, with the unmodified clone green as the control:

| | Evasion | Before | After |
|---|---|---|---|
| E1 | `from importlib import reload` + bare `import_module(...)` | missed | **caught** |
| E2 | `importlib.reload(training_gym.datasets.export)` (dotted attribute) | missed | **caught** |
| E3 | `importlib.reload(sys.modules["..."])` (subscript) | missed | **caught** |
| E4 | reload in `jarvis/tests/_test_support/reloader.py` (glob was non-recursive) | missed | **caught** |
| E5 | reload in the repo-root `tests/` tree (the second tree CI collects) | missed | **caught** |
| E6 | `from pkg import mod as alias`, then `reload(alias)` | missed | **caught** |
| — | unmodified clone (control) | 3 passed | 3 passed |

Also fixed from the MINOR list: module-level classes declared under `if`/`try` are now
counted; the alias map no longer mis-keys a plain `import a.b.c`; and the nested runs pass
`-p no:randomly`, so the two "known orders" stay the orders the guard names even if an
ordering plugin is ever installed. The remaining MINORs are recorded in §8.

Two further checks after the rewrite: a reload hidden inside a helper function and reached
through a **fixture** is caught; and all six modules that bind names from
`training_gym.datasets.export` at import time were run in **10 orders** (both pair
directions, full reverse, and eight seeded shuffles) — 468 passed every time.

## 12 — Why the workflow is unchanged

A dedicated `D39 order-independence sentinel` step was added to the authoritative `tests`
job, so the gate would appear as its own named remote result. It was **reverted**, because
the full suite then failed three of S5E's and M61.2's own reality gates:

```
test_ci_entrypoint_reality_v69_s5e.py::test_the_authoritative_command_names_both_trees
  AssertionError: expected exactly one pytest invocation in the authoritative job, got 2
test_ci_workflow_v69_m612.py::test_an_authoritative_job_runs_the_complete_suite
  Failed: the authoritative job runs a subset: python -m pytest -q ... test_order_isolation_...
```

Those gates are deliberate: the authoritative job runs **one** pytest invocation over the
complete suite, and nothing that looks like a subset. Buying a prettier job list by relaxing
them would trade a real invariant for a cosmetic one.

Nothing is lost. `jarvis/tests/test_order_isolation_d39_v69_s5f.py` lives in `jarvis/tests`,
which the authoritative command names, so all three sentinel nodes are collected and run on
every CI run — verified: `pytest --collect-only jarvis/tests tests` reports 3 matching nodes.
The D39 gate executes remotely; it simply is not a separately named step.
