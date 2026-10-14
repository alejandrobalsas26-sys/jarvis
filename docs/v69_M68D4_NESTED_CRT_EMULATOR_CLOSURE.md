# V69 M68D.4 — nested native CRT emulator closure

**Milestone.** M68D.4. A narrowly scoped successor to generation 71 that repairs
the remaining nested Windows CRT **test-harness** defect, preserves every
production security invariant generation 70 and 71 established, and qualifies
the exact sealed successor SHA on a real Windows runner.

**This is not master integration.** Master is untouched, local and remote, at
`af54fb95c6f0cfb4daa84e08b6fde6dff9a62118`.

**No production code changed.** `jarvis/core/` and `jarvis/tools/` are
byte-identical to generation 71, which is itself byte-identical to generation 70
in those trees. The repair is entirely in test and campaign code, plus one
workflow-guard addition in a test file.

---

## 1 · The historical CI run this milestone answers

| field | value |
|---|---|
| run id | **38160730836** |
| event | `workflow_dispatch` |
| branch | `jarvis-v69-m68d3-native-windows-simulator` |
| head SHA | `530d2ea124f858324e0cf4ffce017c1edaf2780d` (generation 71) |
| created | 2026-10-11T17:41:57Z |
| conclusion | **failure** |
| Linux mandatory jobs | **10/10 SUCCESS** |
| Windows portability (3.11, blocking) | **FAILURE** — job id `114532907615`, runner `windows-latest` |
| Windows counts | **425 passed / 9 failed / 17 skipped** (collection 451) |
| Windows Control Plane | **PASS / 0** |
| Windows NEWLINE_POLICY | **PASS** |

Recovered from the runner itself (`gh run view --log --job 114532907615` and
`gh api .../actions/jobs/114532907615`), not from a previous assistant's
summary.

### The Windows job never reached its last two steps

| # | step | outcome |
|---|---|---|
| 1 | Set up job | success |
| 2 | `actions/checkout@v4` | success |
| 3 | `actions/setup-python@v5` | success |
| 4 | Report the checkout's newline configuration | success |
| 5 | Install dev profile (constrained) | success |
| 6 | Control Plane verifier (must be PASS / 0 on Windows) | **success** |
| 7 | Source-integrity and portability suites | **failure** |
| 8 | Dependency authority and consistency checks (build + `--dist` manifest scan) | **skipped** |
| 9 | Import smoke (no service, no model) | **skipped** |

So after four real Windows attempts the `--dist` manifest/secret scan and the
import smoke have **still never executed**. A passing suite step does not prove
either one, and this milestone does not claim them until the runner reports
them.

### The nine failures, by exact node ID

All nine are in
`jarvis/tests/test_cross_platform_source_integrity_m68d2.py::TestTheRuntimeIsNotDoubleSimulated`:

| node | failure |
|---|---|
| `test_the_probe_is_a_measurement_and_not_a_constant` | `AssertionError: the probe did not see the emulated CRT: FAILED: the explicitly-binary descriptor is not byte-exact either (4 of 8 bytes)` |
| `test_a_host_whose_binary_mode_is_also_broken_is_refused` | `AssertionError: the emulated host is not sane` |
| `test_the_native_branch_keeps_the_production_read_byte_exact` | `RuntimeError: the native text-mode capability was not measured … (4 of 8 bytes)` |
| `test_the_native_branch_still_shows_the_text_mode_behaviour` | `RuntimeError: … was not measured … (4 of 8 bytes)` |
| `test_a_shim_that_strips_the_binary_flag_is_caught` | `RuntimeError: … was not measured … (4 of 8 bytes)` |
| `test_double_modelling_the_host_is_caught[double_transforms]` | `RuntimeError: … was not measured … (4 of 8 bytes)` |
| `test_double_modelling_the_host_is_caught[both_translations_active]` | `RuntimeError: … was not measured … (4 of 8 bytes)` |
| `test_a_leaked_patch_is_caught_by_the_next_measurement` | `AssertionError: a leaked shim is still corrupting reads after its block` |
| `test_the_runtime_restores_itself_through_an_exception` | `AssertionError: assert RuntimeError('… was not measured … (4 of 8 bytes)') is RuntimeError('staged')` |

**One root cause, no second cause.** The verdict string
`the explicitly-binary descriptor is not byte-exact either (4 of 8 bytes)`
appears 20 times in the job log and is the proximate or transitive cause of all
nine. Five died on the fail-closed refusal it correctly triggered.

---

## 2 · The confirmed root cause, independently reproduced

### What generation 71 did

M68D.3 repaired `windows_text_runtime()` exactly as the M68D.2 lesson
prescribes — branch by **measurement**, stand down where the host already
provides the behaviour. **It worked:** all 20 of CI 38007992655's failures
closed, zero failures in `TestBinaryByteIdentity`, Windows 376 → 425 passed.

It then failed nine tests by committing the **same error one layer up**. To
exercise the *native* branch from a POSIX host, M68D.3 added
`host_with_a_text_crt()` — an emulator that replaces the **host** at the layer
the capability probe measures. It installed its CRT unconditionally, and its
open did:

```python
fd = host_open(path, flags & ~bit, *args, **kwargs)
```

On Linux `host_open` is POSIX and `bit` is `SIMULATED_O_BINARY`, a bit derived
from `_HOST_FLAG_BITS` to be one this host spends on no `O_*` flag — so the
line is bookkeeping. **On the real Windows runner `host_open` IS the native
`os.open` and `bit` IS the real `os.O_BINARY`.** The emulator therefore
stripped the binary flag from its own probe's explicitly-binary descriptor, the
native CRT handed back a text-mode descriptor, and the probe condemned itself.

**The fail-closed refusal was correct. The emulator was wrong.** The probe is
not weakened to accept the corrupted result.

### The generalisation: the no-op rule is recursive

Every layer that imitates a platform capability — the simulator, *and* the
emulator that exists so the simulator's other branch can be tested, *and* the
inverse fixture beside it — must ask *does the host already provide this?* and
stand down if so. Fixing layer 1 by measurement does not protect layer 2.

### Reproduced on Linux by nesting, before any edit

Nesting puts the inner emulator on a host that already translates, which is
exactly what Windows is.

| | `outcome` | `unflagged` | `binary` | `translates` | `measured` |
|---|---|---|---|---|---|
| level 0 (real Linux host) | `BYTE_EXACT` | `b"A\r\nB\r\n\x1aC"` | `None` | `False` | `True` |
| level 1 (one emulator) | `NATIVE_TEXT` | `b"A\nB\n"` | `b"A\r\nB\r\n\x1aC"` | `True` | `True` |
| level 2 (nested) **before** | `FAILED: the explicitly-binary descriptor is not byte-exact either (4 of 8 bytes)` | `b"A\nB\n"` | `b"A\nB\n"` | `False` | `False` |
| level 2 (nested) **after** | `NATIVE_TEXT` | `b"A\nB\n"` | `b"A\r\nB\r\n\x1aC"` | `True` | `True` |

Measured on the real host: `os.O_BINARY` absent, `WIN_O_BINARY == 0x8000 ==
SIMULATED_O_BINARY`, `os.open` flags as shown, and at level 2 before the repair
`windows_text_runtime()` raised
`RuntimeError: the native text-mode capability was not measured, so neither
branch can be entered honestly: FAILED: … (4 of 8 bytes)` — **the real runner's
verdict string, byte-for-byte.**

---

## 3 · The minimum production-safe repair

`host_with_a_text_crt()` now **measures the current host first** and then:

**measurement FAILS** — refuses to enter. `RuntimeError`, no yielded verdict,
nothing patched, `CRT_EMULATOR.refusals` incremented. A failed capability
measurement is never converted into a native or synthetic success.

**the host already translates** (real Windows, or any nested entry) — selects
the native path and **stands down completely**:

- installs no CRT shim;
- does not alter `os.open`, `os.read` or `os.close`;
- does not replace `_REAL_OPEN`, `_REAL_READ` or `_REAL_CLOSE`;
- does not change `os.O_BINARY`'s presence or value;
- records the measured `NATIVE` verdict and yields it;
- returns before the shim construction below it.

The native proofs then run against the **real CRT** on Windows — genuine
PROOF A / PROOF B rather than an emulation of them.

**the host is byte-exact** (POSIX) — installs the existing controlled synthetic
CRT unchanged: the same `crt_open` / `crt_read` / `crt_close`, the same
synthetic descriptor tracking, the same `crt_text_fds.discard(fd)` on close so a
reissued descriptor number cannot inherit a stale text-mode designation. It
then **re-probes and requires `NATIVE_TEXT`**, raising if the CRT it just built
did not produce the host it promised — a fail-closed hardening generation 71
did not have.

In every case the complete previous runtime state is restored in a `finally`,
exceptions propagate unchanged, and descriptor cleanup is preserved.

### The `CrtEmulatorLedger`

A new non-vacuity record, the host-layer counterpart of `TextRuntime`:
`entries`, `installs`, `noops`, `refusals`. `entries` moves on every use;
`installs` moves only when a shim was really put in place. **A measured no-op is
therefore `entries` without `installs`** — a behavioural fact a future edit
cannot fake by keeping the early-return branch and patching anyway. Two
ledgers exist: `CRT_EMULATOR` and `BYTE_EXACT_EMULATOR`.

---

## 4 · The §10 inverse control, and why it was necessary

`test_the_probe_is_a_measurement_and_not_a_constant` proved the capability
verdict is **measured** by contrasting the real host against
`host_with_a_text_crt()`. That is a real contrast on POSIX, where the real host
is byte-exact. **It is not on Windows:** once the emulator correctly stands down
on a translating host, both readings are `NATIVE_TEXT` and the test would keep
passing while distinguishing nothing.

The test is neither weakened nor deleted. The missing half is supplied:
`host_with_a_byte_exact_crt()` presents the **inverse** capability to the same
measurement function, and the two verdicts must differ.

```
exactly one fixture does real work on any given host; the other stands down
    POSIX    → text fixture INSTALLS,  inverse fixture stands down
    Windows  → text fixture stands down, inverse fixture INSTALLS
either way: NATIVE_TEXT for one and BYTE_EXACT for the other, same process
```

The test asserts the contrast (`emulated.outcome != exact.outcome`, pinned to
`(NATIVE_TEXT, BYTE_EXACT)`) **and its non-vacuity from the ledgers**:
`installed == 1` and `stood_down == 1`. Two no-ops cannot satisfy it.

The inverse fixture is deliberately **not** a general-purpose OS emulator (§10).
It patches `os.open` and nothing else, it exists only to feed the capability
probe, `windows_text_runtime()` is never entered inside it, and
`test_the_host_emulator_is_still_native_aware` fails if it ever grows an
`os.read =` or `os.close =`. The recursive no-op rule applies to it too: on a
host already measured byte-exact it installs nothing.

### What each test proves about which host behaviour

| test | proves |
|---|---|
| `test_the_probe_is_a_measurement_and_not_a_constant` | the SAME probe returns `NATIVE_TEXT` for a translating host and `BYTE_EXACT` for a byte-exact one, in one process, with one install and one stand-down |
| `test_a_nested_emulator_is_a_true_noop_on_a_translating_host` | a second emulator on a translating host installs nothing, corrupts no binary read, and adds no second translation |
| `test_the_native_branch_keeps_the_production_read_byte_exact` | on a translating host, `binary_available=True` leaves a 10240-byte production read byte-exact (PROOF A; the real CRT on Windows) |
| `test_the_native_branch_still_shows_the_text_mode_behaviour` | the same host truncates an unflagged read to 26 bytes with no synthetic transform layered on (PROOF B) |
| `test_a_shim_that_strips_the_binary_flag_is_caught` | the M68D.2 defect, run, observing 26 bytes and digest `b858da80d8a5…` |
| `test_a_host_whose_binary_mode_is_also_broken_is_refused` | translation observed is not enough — the binary descriptor must be exact or the host is refused |
| `test_the_host_emulator_refuses_a_host_it_cannot_measure` | both fixtures refuse to enter on an unmeasurable host, patch nothing, and record the refusal |
| `test_the_host_emulator_restores_itself_through_an_exception` | both fixtures restore the full runtime and the measurement **object** through an exception, nested and not, leaking no descriptor |

---

## 5 · The mandatory nested regression, and its falsification

`TestTheRuntimeIsNotDoubleSimulated::test_a_nested_emulator_is_a_true_noop_on_a_translating_host`
runs on Linux **and** Windows, is in `MANDATORY_CROSS_PLATFORM_INVARIANTS`, and
is behind no skip. It performs the eleven required steps:

1. records the pristine runtime — `os.open/read/close`, `_REAL_*`, `NATIVE`,
   `NATIVE_TEXT_TRANSLATION_OBSERVED`, `os.O_BINARY` presence and value, the
   lowest free descriptor number, and the ledger;
2. enters the outer translating-host emulator;
3. proves the outer layer is native-like: `NATIVE_TEXT`, `unflagged ==
   b"A\nB\n"`, `binary == NATIVE_PROBE_BYTES`, and a non-idempotent witness
   read;
4. enters a second nested emulator;
5. proves the inner layer is a **true no-op**: verdict still `NATIVE_TEXT`,
   `installs` unchanged, `noops + 1`, and every runtime object identical;
6. proves it corrupts no binary read — raw `O_RDONLY|WIN_O_BINARY` and a
   production `source_snapshot` both return all 10240 bytes with the right
   digest;
7. proves no additional translation — see below;
8. exits the inner context;
9. verifies the outer context still operates: objects restored, the measurement
   **object** handed back, the witness still translated once, and the outer host
   still truncating an unflagged production read to 26 bytes;
10. exits the outer context;
11. verifies the pristine runtime back by value **and** the measurement by
    identity, with no leaked descriptor and at most one install for two entries.

### The non-idempotent translation witness

Step 7 cannot use the probe fixture: `b"A\r\nB\r\n\x1aC"` collapsed twice gives
the same answer as collapsed once. The witness is `b"A\r\r\nB"`:

```
one CRLF collapse  → b"A\r\nB"     ← required
two collapses      → b"A\nB"       ← a nested layer translating on top
```

The real Windows CRT produces the same one-pass result (it removes a `\r` only
when a `\n` follows). The assertion is checked structurally over the **parsed
tree**, comparing AST constant *values*, because the byte literal is a STRING
token that `_no_strings_of` drops and a raw-text scan would match the
detector's own copy.

### Before / after falsification — the mandatory gate

The exact generation-71 body of `host_with_a_text_crt` was recovered from git
(`git show 530d2ea:…`), `exec`'d into the module namespace, and the new tests
run against it.

| new test | against generation 71 | against the repair |
|---|---|---|
| `test_a_nested_emulator_is_a_true_noop_on_a_translating_host` | **FAILED** — `the inner emulator re-modelled a host that already translates: FAILED: the explicitly-binary descriptor is not byte-exact either (4 of 8 bytes)` | PASSED |
| `test_the_host_emulator_refuses_a_host_it_cannot_measure` | **FAILED** — `host_with_a_text_crt entered on a host it could not measure` | PASSED |
| `test_the_probe_is_a_measurement_and_not_a_constant` | **FAILED** — `0 of the two capability fixtures presented a host; exactly one must` | PASSED |
| `test_the_host_emulator_restores_itself_through_an_exception` | PASSED | PASSED |

**Stated honestly:** three of the four are falsified. The nested regression
fails with the real runner's verdict string verbatim, which is the gate §7
requires. The restoration control **passes** against generation 71 — generation
71 did restore state correctly through exceptions — so it is a *new control for
§9*, not a falsification of this defect, and is not claimed as one. The probe
contrast's generation-71 failure is a ledger-absence failure (generation 71 has
no `CRT_EMULATOR`), which is a true falsification of the new assertion but a
weaker form of evidence than the nested test's.

---

## 6 · Mutation evidence

Preserved campaigns, all re-run against this tree, **sequentially and never
concurrently with an authoritative suite**:

| campaign | result |
|---|---|
| M68C | **52/52** detected, 0 survivors, 0 anchor errors |
| M68D | **72/72** detected, 0 unexplained survivors, 0 anchor errors |
| M68D.1 | **40/40** detected, 0 unexplained survivors, 0 anchor errors |
| M68D.2 | **37/37** detected, 0 unexplained survivors, 0 anchor errors |
| M68D.3 | **28/28** detected, 0 unexplained survivors, 0 anchor errors |
| **M68D.4 (new)** | **21/21** detected, 0 unexplained survivors, 0 anchor errors |

All 229 pre-existing anchors still resolve uniquely against this tree; none was
discarded or weakened to accommodate the repair.

`scripts/mutation_campaign_m68d4.py` covers every §12 semantic:

| § 12 semantic | mutation |
|---|---|
| native host always emulated | `A_native_host_is_always_emulated` |
| (mirror) emulator never installs | `A_the_emulator_always_stands_down` |
| native capability ignored | `B_the_native_capability_is_ignored` |
| second capability measurement skipped | `B_the_second_measurement_is_skipped` |
| failed probe accepted as valid | `H_a_failed_probe_is_accepted_as_valid` |
| binary flag unconditionally stripped | `C_the_binary_flag_is_unconditionally_stripped` |
| native branch wrongly patches `os.open` | `F_the_native_branch_patches_os_open` |
| native branch wrongly patches `os.read` | `G_the_native_branch_replaces_os_read` |
| second nested emulator performs a transformation | `D_the_nested_emulator_transforms_as_well` |
| state not restored after exception | `I_state_is_not_restored_after_an_exception` |
| inner context corrupts outer context | `J_the_inner_context_corrupts_the_outer_one` |
| negative control made vacuous | `K_the_probe_contrast_is_made_vacuous`, `K_the_inverse_fixture_strips_instead_of_adding`, `K_the_inverse_fixture_does_not_verify_itself`, `M_the_nested_negative_control_is_made_vacuous` |
| nested test's byte identity assertion removed | `E_the_nested_byte_identity_assertion_is_removed`, `E_the_second_shim_is_no_longer_forbidden`, `E_the_single_translation_witness_is_removed` |
| mandatory nested invariant removed from inventory | `L_the_nested_invariant_leaves_the_inventory` |
| source-integrity suite removed from CI | `L_the_source_integrity_suite_leaves_ci` |
| required Windows job made optional | `L_the_windows_job_becomes_advisory` |

§12's "native branch wrongly patches `os.open`/`os.read`" for the **simulator**
already exist with their original anchors as M68D.3's
`D_strip_the_binary_flag_on_a_native_host` and
`D_double_transform_the_native_read`. The M68D.4 entries above mutate the
**host emulator's** native branch, which is new code and was unguarded.

### Three mutations survived the first run. Each was a real gap.

Recorded because the fixes are the substance of this milestone, not noise.

1. **`I_state_is_not_restored_after_an_exception` survived.** `NativeTextRuntime`
   is a *frozen dataclass*, so `==` is field-wise. An inner context whose
   `finally` restored **nothing** left its own measurement in place — same
   verdict, same bytes, **different object** — and compared equal to the outer
   one. Fixed by asserting **identity** (`NATIVE is outer_native`,
   `NATIVE is pristine_native`, `NATIVE is nested_native`) alongside equality.
   *An equal verdict is not a restored one.*
2. **`E_the_nested_byte_identity_assertion_is_removed` survived.** The needle
   `assert _read_whole(target, O_RDONLY|WIN_O_BINARY) == self.FIXTURE` occurs
   **twice** in the nested test — once inside the inner context, once after it.
   Deleting the load-bearing one left the other to satisfy a membership test:
   exactly the "a string found somewhere else must not count" hazard of §11.
   Fixed by requiring `count >= 2`.
3. **`K_the_probe_contrast_is_made_vacuous` survived its behavioural mapping.**
   Forcing the inverse fixture to always stand down is invisible on POSIX,
   where standing down is the *correct* branch; it is caught behaviourally only
   on Windows, where `installed == 1` then fails. Remapped to the structural
   detector, with the platform asymmetry documented in the campaign.

---

## 7 · Known residual limitations

- **`cas_write_text(mode="a")` remains OPEN and is NOT repaired here.** The
  append path is text-mode, so `bytes_written` undercounts on Windows. The fix
  fans out to four places and is deliberately out of scope;
  `TestTheAppendDefectIsStillOpen` keeps it pinned.
- **The Bandit suppression detector still matches the substring `nosec`.** A
  word such as "nanosecond" turns the gate red, and the obvious fix would grant
  a real suppression. Documented, not broadened by this milestone.
- **CI cannot witness the L3 process limits** — runners have no sandbox
  backend, so a green CI run does not verify them.
- **Linux cannot witness the native branch for real.** It is exercised through
  an emulator; only the Windows runner takes it against the real CRT. That is
  precisely why the Windows job must stay blocking.
- The `--dist` manifest/secret scan and the import smoke on Windows remain
  **unverified** until a runner actually executes steps 8 and 9.

## 8 · Local validation, measured

CI-parity interpreter: **CPython 3.11.16**, `/home/kali/Downloads/jarvis/.venv-ci311`,
built outside the repository tree because every in-tree `.venv*` is 3.14.

### The focused suites (§13)

| Suite | Result |
| --- | --- |
| `test_source_integrity_m68c.py` | 92 passed |
| `test_trust_boundary_m68d_h02_source_identity.py` | 59 passed, 2 skipped |
| `test_trust_boundary_m68d_h05_portability.py` | 69 passed |
| `test_windows_portability_closure_m68d1.py` | 70 passed |
| `test_cross_platform_source_integrity_m68d2.py` | **174 passed** |
| `…m68d2.py` + `…h05_portability.py` together | 243 passed |
| `TestTheRuntimeIsNotDoubleSimulated` (holds the nine) | 21 passed |

All nine node IDs of run 38160730836 pass, with no `xfail`, no added skip and
no deselection.

### Both authoritative trees

    python -m pytest -q --tb=short jarvis/tests tests      # from the repo ROOT

**13254 passed · 50 skipped · 0 failed · 13304 collected**, in 730s.

The generation-71 reference was 13246 / 50 / 13296. The collection moves by
**+8**, and the movement is accounted for exactly rather than pinned:

- **+7** new tests in `test_cross_platform_source_integrity_m68d2.py`, each
  collecting exactly one case (none is parametrised).
- **+1** new `parametrize` case in `test_trust_boundary_m68d_h05_portability.py`
  — `test_it_runs_the_portability_relevant_suites` now also requires
  `test_cross_platform_source_integrity_m68d2.py` to be named in the Windows
  job. That is the §11 absent-control for *"removes the source-integrity suite
  from CI"*, and it is a behavioural requirement on `ci.yml`, not a string
  search.

Skips are **unchanged at 50**: nothing was traded for a skip. A name-diff of
the suite against `530d2ea` lists 7 added and **0 removed**.

### The canonical scientific selection

Run **alone**, from `jarvis/` — the manifest's module paths are relative to the
package root, and the working directory is plan-affecting here.

    python jarvis/scripts/verify_m62_scientific_suite.py --print-invocation

**3179 passed · 2 skipped · 0 failed · 3181 collected**, in 213s — identical to
generation 71, as it must be: `graders/` and `training/` are byte-frozen and
this milestone touches neither. No scientific authority was spent, no holdout
was read, no training or evaluation ran.

### Static gates

| Gate | Result |
| --- | --- |
| `ruff check .` | All checks passed |
| `compileall core tools scripts aura main.py __main__.py` | clean, exit 0 |
| `git diff --check` | clean |
| bidi / invisible-character scan | 0 hits across all 4 changed files; no CRLF |
| `bandit -r core tools -ll -q` | **HIGH 0 · MEDIUM 0** |
| Bandit LOW | **490**, exactly the recorded baseline |

### Mutation falsification

Campaigns are run **sequentially and never concurrently with an authoritative
suite** — they mutate tracked files in place while `census()`-style tests spawn
a nested pytest that reads them, which is how generation 71 measured one bogus
failure.

| Campaign | Detected |
| --- | --- |
| M68C | 52 / 52 |
| M68D | 72 / 72 |
| M68D.1 | 40 / 40 |
| M68D.2 | 37 / 37 |
| M68D.3 | 28 / 28 |
| **M68D.4** | **21 / 21** |
| **Total** | **250 / 250** |

**0 survivors, 0 anchor errors**, and all 229 inherited anchors still resolve
uniquely — no old anchor was discarded to buy a clean count, and no mutant was
deleted to reach one.

## 9 · Exact Windows qualification result

Recorded in the final session report against the **sealed** generation-72
candidate SHA, because a run of the sealed tree cannot exist inside the commit
it measures — the generation-71 document sets the same precedent.

**Windows is not qualified until the job's last two steps conclude.** In all
four previous attempts the suite step failed first, so these have *never*
executed on Windows and no passing suite step may be read as evidence for them:

1. the dependency-authority / manifest-and-secret scan with `--dist`, and
2. the import smoke.

The job must stay blocking, with no `continue-on-error`, no broad Windows skip
and no CI exclusion.
