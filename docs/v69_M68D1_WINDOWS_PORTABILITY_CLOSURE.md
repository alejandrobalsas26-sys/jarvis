# V69 M68D.1 — Windows portability closure

Milestone branch: `jarvis-v69-m68d1-windows-portability-closure`
Base: `cc8a7dda6efd20b371459a51f4f687711972a88a` (M68D, generation 68)
Master at the time of writing: `af54fb95c6f0cfb4daa84e08b6fde6dff9a62118`, untouched.

**M68D did not fail.** Its development evidence was valid: ten Linux CI jobs
green, five external P1 findings independently reproduced, 72/72 mutations
detected. What failed was its *integration qualification* — the first time a
real Windows runner ever executed the `windows-portability` job it had
authored. M68D.1 is the successor that closes that gap. Generation 68 is
published and sealed; nothing in it is amended.

## 0. The incident

| | |
|---|---|
| Branch CI run | **37695660326** (`workflow_dispatch`, exact head SHA) |
| Candidate | `cc8a7dda6efd20b371459a51f4f687711972a88a` |
| Governed subject | `1fb82664ea21c662d5ef227dfc0df133159cefb4` |
| Generation | 68, snapshot `state/m62/snapshots/0068-m68d-trust-boundary.json` |
| Snapshot sha256 | `411de76294680578166d93f90d59c40b82cb16e2cac74965861d9f564d7e49d0` |
| Linux | **all 10 jobs green** |
| Windows runner | `windows-2025-vs2026` / `windows-latest`, CPython **3.11.9** |
| Windows result | **173 passed / 25 failed / 9 skipped** |

The exact-SHA ceremony stopped correctly. **Master was never touched.**

## 1. Why the Control Plane passing matters

Step 6 of the Windows job ran the canonical verifier under `core.autocrlf=true`
— the hostile Windows default, left in place on purpose — and printed:

```
M62_CONTROL_PLANE_VERIFY : PASS
NEWLINE_POLICY           : PASS
PROBLEMS                 : 0
```

H05's central claim is therefore **genuinely proven on real Windows**: the
`.gitattributes` `-text` pins deliver `PROGRESS.md: text: unset` and
`state/m62/current.json: text: unset` on a CRLF-defaulting checkout, and the
131 false "newline" problems measured on a converted clone become zero.

Nothing in M68D.1 reverts or weakens that work. The newline pinning was not the
problem; it is the part that already worked.

## 2. The three failure groups

All 25 failures were in the **test suites**, and every one was a POSIX
assumption in a fixture rather than a defect in production.

### GROUP A — 14 failures. Windows mandatory open-handle semantics

CPython's `os.open` goes through the CRT, which requests
`FILE_SHARE_READ | FILE_SHARE_WRITE` and **not** `FILE_SHARE_DELETE`. So while
this process holds a descriptor on a path, Windows refuses to rename over it
(`WinError 5`) or to delete it (`WinError 32`). The H02 adversarial fixtures
stage exactly that mutation: they hold the snapshot's own descriptor and then
`os.replace` / `unlink` the path underneath it.

On POSIX that mutation succeeds and *proves* that path mutation cannot alter a
held observation. On Windows the scenario **cannot be staged at all**.

This is explicitly **not** evidence that H02 should close the descriptor before
hashing. That composition — render from one opening, hash a later one — is the
original TOCTOU defect H02 exists to remove. Making the POSIX fixture runnable
on Windows by releasing the descriptor would restore the bug.

### GROUP B — 10 failures. Runtime fixture newline translation

`.gitattributes -text` pins **tracked** byte-sealed artifacts. It says nothing
about a temporary file a test writes at runtime. `Path.write_text` writes in
text mode with `newline=None`, which translates `"\n"` to `os.linesep`.

So a fixture written as `"x = 1\n"` is a **seven**-byte file on Windows, and a
digest taken over `b"x = 1\n"` no longer describes it. Measured consequences on
the runner: `assert 7 == 6` on `size_bytes`, mismatched `sha256`,
`assert b'line\r\n…'`, and `WriteStatus.REJECTED_STALE` where `APPLIED` or
`VALIDATED_NOT_APPLIED` was expected — because the CAS precondition had been
computed from the LF literal.

A related subtlety that is **correct and must stay**: `SourceSnapshot.text()`
decodes the captured bytes and performs no newline translation. That is what
makes the content and the digest describe each other, and it is why a CRLF
fixture also shifted `content_chars_total`.

### GROUP C — 1 failure. No POSIX executable bit

`test_os_access_disagrees_with_git_when_the_worktree_bit_is_cleared` needs a
**clearable** executable bit: `chmod(0o644)` must make `os.access(X_OK)` false.
Windows `chmod` honours only `FILE_ATTRIBUTE_READONLY`. The test failed its own
non-vacuity guard — *"the working-tree bit was not actually cleared"* — which is
the test being **honest**, not a defect in the control.

## 3. Production bug vs test-contract portability

| | Verdict |
|---|---|
| `source_snapshot` / `SourceSnapshot` / `identify_snapshot` | **No defect.** One `os.open(O_RDONLY)`, digest over the captured bytes, nothing reopened. |
| `digest_file` / `digest_bytes` | **No defect**, and deliberately not touched. Normalising newlines would stop a byte seal being a byte seal. |
| `cas_write_text`, whole-file (`mode="w"`) | **No defect.** Stages `payload` through a **binary** temp handle, so the bytes that land are the bytes the receipt describes, on every platform. |
| `cas_write_text`, append (`mode="a"`) | **Defect — found by M68D.1, DEFERRED.** See §8. |
| Control Plane newline policy and Git-mode authority | **No defect.** Both proven on the real runner. |
| The 25 failures | **Test-contract portability**, all of them. |

## 4. Local reproduction (§12)

The authoritative evidence is a GitHub Windows runner. Before changing a single
test, all three classes were reproduced **locally on Kali** with a measurement
instrument (`WINSIM`) that patches exactly the three kernel behaviours:

* `lock` — refuse `os.replace`/`os.unlink` on a path this process holds a
  descriptor for. Liveness is read from `/proc/self/fd`, not counted: a first
  version counted `os.open`/`os.close` and was wrong, because `os.fdopen` hands
  the fd to the io layer whose `close()` does not go through `os.close`. Every
  temp file `cas_write_text` staged stayed "held" forever and its own
  `os.replace` was denied — **19 extra failures the real runner did not have**.
* `newline` — force `newline="\r\n"` for text-mode writes. This is an **exact
  byte reproduction**, not an approximation: that is what Windows emits for
  `newline=None`.
* `execbit` — the full Windows `chmod` semantic: only the read-only attribute is
  honoured, read access is never removed, and `os.access(X_OK)` is true for
  anything readable.

Measured over the **three** suites the Windows job ran at `cc8a7dd`, before
any repair:

| | Result |
|---|---|
| Control, no simulation | 205 passed / 2 skipped / **0 failed** |
| `LOCAL_REPRO_A` (`lock`) | **6 failed** — all H02, all the rename/unlink mechanism |
| `LOCAL_REPRO_B` (`newline`) | **13 failed** — 7 in M68C, 6 in H02 |
| `LOCAL_REPRO_C` (`execbit`) | **3 failed** — incl. the exact Group C test |
| Combined | **18 failed** |

**18 of the 25 reproduced.** This is a simulation and does not replace Windows.
The gap is accounted for, not hand-waved:

* `os.name` cannot be faked on Linux, so three `POSIX_MODES_ENFORCED`-guarded
  M68C tests that **skip** on the real runner instead **fail** under the
  simulation. One of those (`test_an_unreadable_file_yields_no_digest`) is the
  single remaining simulated failure after the repair, and it is an artifact of
  the instrument, not of the repository. Established by inspection, not
  measurement: `os.name != "nt"` is false on Windows, so the guard fires.
* `python-docx` and `pdfplumber` are absent from `requirements/dev.txt`, so the
  runner's `importorskip` tests skip where this host's may not.
* Host path semantics (`Path.home() / "Downloads"` sandbox roots) are not
  modelled at all.

## 5. The repair

### Group A — portable adversarial proof (§6, §7, §8)

A new **measured** capability probe, `jarvis/tests/_test_support/platform_capabilities.py`:

* It **performs** each operation once in a temp directory and records what the
  kernel said. It never reads `sys.platform`, `os.name` or `platform.system()`.
* It holds the descriptor **exactly as production does** —
  `os.open(path, os.O_RDONLY)`, plain CPython. It deliberately does **not**
  request `FILE_SHARE_DELETE`; doing so would let the POSIX attack shape run on
  Windows against a handle production never opens, and the test would then be
  evidence about the probe.
* An **unmeasured** capability defaults to ABSENT, so a broken probe causes a
  skip rather than a failure for a reason that is not about JARVIS.

Measured by the probe under the simulation — the same two codes the real runner
produced:

```
replace_over_open_path        REFUSED: PermissionError errno=13 winerror=5
unlink_open_path              REFUSED: PermissionError errno=13 winerror=32
in_place_rewrite_of_open_path PERMITTED
mode_bits_remove_read         REFUSED: chmod(0o000) left the file readable
exec_bit_observable           REFUSED: X_OK was True at 0o755 and True at 0o644
```

The key insight: **an in-place rewrite of the held inode is permitted on every
supported platform, and it is the STRONGER adversary.** An atomic replacement
cannot disturb a held descriptor at all — that is why `stable` stays `True`
through one. An in-place rewrite changes the very bytes the observation is
reading, which is the case `stable` and `recheck()` exist for.

So where the rename cannot be staged, the suite stages the in-place rewrite and
proves the same safety conclusion, including the one the finding was really
about: a raced reader cannot produce a precondition under which the unread human
edit is destroyed. It is refused one step earlier than in the rename case — the
observation has no digest at all.

Tests parametrised over the mechanisms the host measured (so they **run** on
Windows with a reduced set rather than skipping):

* `test_a_post_acquisition_mutation_cannot_alter_the_observation` (4 → 1 params)
* `test_two_concurrent_snapshots_of_one_file_agree` (2 → 1 params)

New cross-platform companions:

* `test_an_in_place_rewrite_mid_read_cannot_split_content_from_digest`
* `test_an_in_place_race_cannot_state_a_precondition_that_destroys_the_edit`
* `test_a_derived_parse_over_changed_bytes_is_refused_not_returned`
* `test_a_derived_parser_never_reopens_the_source_path` — behavioural, where
  every sibling proof is a source-text scan

### Group B — byte-explicit fixtures (§9, §10)

A `_write(target, text) -> bytes` helper in both byte-identity suites writes
`text.encode("utf-8")` through `write_bytes` and **returns the bytes**, so
callers assert against the return value instead of re-encoding the literal.
All **51** fixture writes converted (47 in M68C, 4 in H02); `.write_text(` no
longer appears in either suite at all. Production hashing is untouched.

Non-vacuity, meaningful on Linux:
`test_a_translating_text_write_is_not_byte_equal_to_an_explicit_one` simulates
the translation with `newline="\r\n"` and reproduces the measured Windows
symptom in one test — different length, different `sha256_file`, and
`REJECTED_STALE` where the LF precondition expected `APPLIED`.

### Group C — capability-aware, control unweakened (§11)

One skip, on the DISAGREEMENT witness only, gated on the measured
`EXEC_BIT_OBSERVABLE`. Everything about the control keeps running on every
platform: `update-index --chmod=+x` is an index operation, the sibling test
proves the Git answer is independent of `os.access` even when `os.access` says
true for everything (which *is* the Windows case), and
`test_no_repository_executable_invariant_uses_os_access` proves the verifier
never consults the filesystem. **No `if os.name == "nt": return` was added to
any production verifier.**

## 6. Capability skips introduced, and why (§13, §14)

**This milestone did not turn 25 failures into 25 skips.**

| Suite | Capability gates | Added by M68D.1 |
|---|---|---|
| `test_trust_boundary_m68d_h02_source_identity.py` | 8 | 8 |
| `test_source_integrity_m68c.py` | 3 | 0 (pre-existing, M68D) |
| `test_trust_boundary_m68d_h05_portability.py` | 1 | 1 |
| `test_windows_portability_closure_m68d1.py` | 0 | 0 |
| **total** | **12** | **9** |

`WINDOWS_FAILURES_CLOSED = 25` → **16 now pass**, 9 skip. M68C's 10 failures
became 10 **passes**: not one new skip was needed there.

Every skip reason is a `POSIX_CAPABILITY_TEST:` string that names the exact
impossible mechanism and ends with the kernel verdict that was measured. There
is no `@pytest.mark.skipif(sys.platform == "win32", reason="Windows")` anywhere.

Enforced by `TestNoSkipWashing`: the gate count is declared per file and
checked structurally; no suite may carry a module-level mark or a module-level
`pytest.skip`; no suite may branch on a platform name (two named, defended
exemptions); every skipped mechanism must have a named cross-platform
replacement that still exists; and the H02 suite must keep at least eight
passing tests per skipped one.

## 7. What still executes on Windows

The job is unchanged in shape — **blocking**, no `continue-on-error`, no
`|| true`, `autocrlf` left at the Windows default — and now runs **four**
suites: the M68C source-integrity suite, the H02 source-identity suite, the H05
portability suite and the new M68D.1 closure suite, plus the Control Plane
verifier, the dependency-authority checks and the import smoke.

`TestWindowsCiStillBlocks` fails if any suite is dropped, if the verifier step
is removed, if the job is marked advisory, if a step gains `|| true` or a pwsh
error preference, or if `autocrlf` is disabled to make the job pass.

### A SECOND latent red, found here and fixed

Fixing the 25 test failures alone would have moved the red **one step to the
right**. The job's step 8 invoked

```
python scripts/check_package_manifest.py
```

and that script declares `--dist` as a **required** argument, so it exits **2**
on its own argument parsing. Measured locally: `exit WITHOUT --dist = 2`, and
`scripts/check_package_manifest.py` line 136 is
`ap.add_argument("--dist", required=True, …)`. The real runner never reached
step 8 because step 7 failed first, so nothing had ever executed it.

The step now builds the dist the way the packaging job does and passes
`--dist`, which also turns the manifest and secret scan into a genuine
**Windows** check of the built artifacts instead of a command that aborts
before doing anything. Pinned by
`test_every_declared_check_can_actually_run`, with its own non-vacuity test and
two campaign mutations (`E_manifest_check_invoked_without_dist`,
`E_manifest_scanned_before_anything_is_built`).

Every other step of the job is accounted for: steps 1–6 are proven by the real
run (they passed), step 7 is what this milestone closes, step 8 is the above,
and step 9 — the import smoke — is unverified on Windows and listed as a
residual.

## 8. Residual limitations

1. **`WINDOWS_REAL_CI = PENDING_INTEGRATION`.** Every result here is Linux
   evidence plus a byte-exact simulation. Only a fresh exact-SHA Windows runner
   can prove the closure. No push was made to obtain one (§22).
2. **The append branch of `cas_write_text` is a DEFERRED DEFECT.** Found by
   M68D.1 while reading `_write_locked`; no test in the 25 asserts it. It writes
   through `open(target, "a", encoding=...)` — a text handle with no explicit
   `newline`. Measured for the payload `b"first\nsecond\r\nthird\n"` (20 bytes):

   ```
   landed under translation = b'first\r\nsecond\r\r\nthird\r\n'  (23 bytes)
   receipt bytes_written    = 20          -> a 3-byte UNDERCOUNT
   the caller's own \r\n    -> corrupted to \r\r\n
   ```

   **Not fixed here, deliberately.** §5 scopes this milestone to making
   *fixtures* byte explicit "without changing production byte identity" and §9
   forbids touching the CAS write path. The repair is `open(target, "ab")` plus
   `handle.write(payload)`, which changes what lands on Windows and also moves
   the mode constant that
   `test_patch_execution_absent_controls_m68c.py::test_the_write_primitive_still_uses_temp_file_plus_replace`
   pins. That belongs to a successor with its own campaign. It is pinned by
   `test_the_append_branch_is_still_the_known_text_mode_write`, which is the
   test to **update, not delete**, when the repair lands.
3. **M68C's `POSIX_MODES_ENFORCED` is inferred, not measured.** It reads
   `os.name != "nt"`. Left exactly as M68D wrote it because it is conservative
   in the safe direction — a POSIX host that ignored mode bits would FAIL the
   test, not sneak past it — and because the published M68D campaign anchors
   `H05_tests_assume_geteuid` on that literal text. Rewriting it would make
   `mutation_campaign_m68d.py` report an anchor error and M68D's 72/72 would
   stop being reproducible. Declared and audited by
   `INFERRED_CAPABILITY_EXEMPTIONS`.
4. **`_text_mode_writes` does not chase aliases.** M68C's `HalfWriter` holds
   `real_open = si.open` precisely so its injected failure mirrors production's
   own append, so the one text-mode write the detector cannot see is the one
   already under a test of its own (limitation 2).
5. **The rename-under-the-reader property is POSIX-provable only.**
   Distinguishing "the parser read the held inode" from "the parser re-resolved
   the path" requires making the path MEAN something else. On Windows the
   nearest cross-platform proof is behavioural — watch the openers — which is
   what `test_a_derived_parser_never_reopens_the_source_path` does.
6. **`WINSIM` is not Windows.** It models three kernel behaviours and nothing
   else: no share-mode subtleties, no path-length limits, no case-insensitivity,
   no `os.name`-gated code.
6b. **Step 9 of the Windows job, the import smoke, has never executed there.**
   It passes locally and its imports are covered by the `dev` profile, but like
   the manifest step it was behind the failing suite step and is unproven on
   Windows.
7. **A pre-existing broken gate was repaired, not inherited.**
   `mutation_campaign_m68c.py` exited **FAIL with 2 anchor errors** at
   `cc8a7dd`: M68D's own H02 work added `identify_snapshot`, which repeats two
   statements of `identify_source` verbatim, so `A_truncated_always_false` and
   `A_absent_gets_a_digest` began matching twice. Both anchors are now
   disambiguated onto `identify_source`, which is what their mapped tests
   exercise. **No M68C production code changed.**

## 9. Mutation evidence (§18)

`jarvis/scripts/mutation_campaign_m68d1.py` — **40 mutations, 40 detected, 0
survivors, 0 anchor errors, 0 duplicate ids**, over five categories: H02
portability, the capability probe, byte-explicit fixtures, Git mode and the
newline policy, and the Windows CI job.

The campaign is where most of this milestone's controls were actually proven —
and the first run **falsified five of them**. Each is now closed by a test that
did not exist before:

| Survivor | Why it survived | Closed by |
|---|---|---|
| `A_refusal_in_the_race_fixture_is_swallowed` | appended ` or True`; the substring `assert refused == [ ]` was still present | pin the statement through its comma, and reject ` or True` in the region |
| `B_unmeasured_capability_defaults_to_true` | `_probe()` overwrites the defaults wherever it works, so on Linux they are invisible | `test_an_unmeasured_capability_defaults_to_absent` reads them as AST literals |
| `B_probe_does_not_hold_the_descriptor` | closed the fd and reopened `os.devnull`; on Linux the answers stay correct | assert no `os.close` / `os.open` **between** holding and staging |
| `C_atomic_replace_helper_translates` | `os.fdopen(fd, "w", …)` is not a `write_text` call, and on Linux text mode emits the same bytes | an AST detector on the **operation**: a text handle opened for writing must name its newline |
| `D_git_mode_failure_assumed_benign` | emptied the `if not modes:` branch; the guard the H05 test checks for was still there | assert the **consequence** — a `report.fail("PATH_INTEGRITY", …)` inside that branch, via AST |

Three further defects were found by M68D.1's own new tests on their first run:
two hand-counted "same-size" payload literals that were not the same size (now
derived from `len(original)`), and a companion test asserting that a derived
parser cannot see an in-place rewrite — which is **false**, because
`capture_bytes=False` makes `stream()` a `dup` of the descriptor and therefore
shares the inode. That is not a defect: it is precisely why that mode requires
`recheck()`, and the invariant is that such a read is **refused**, which is what
the test now asserts.

A fourth, pre-existing: `test_two_concurrent_snapshots_of_one_file_agree`
passed 3/3 under the lock simulation with the instrument reporting **0
denials** — its barrier aligns the three threads' *start*, and the writer's
`os.replace` consistently completed before either reader held a descriptor. The
adversary never overlapped the observation, and nothing in the fixture could
tell the difference. Both halves are now closed.

## 10. Measured gates

| Gate | Result |
|---|---|
| `AUTHORITATIVE_BEFORE` | 12995 passed / 50 skipped / 0 failed (13045) |
| `AUTHORITATIVE_AFTER` | **13072 passed / 50 skipped / 0 failed (13122)** |
| Collection delta | **+77**, fully accounted: +12 in the five M68D suites, +65 the closure suite |
| Between subject and carrier | **5 failed, BY DESIGN** — the five control-plane tests that ask the verifier whether this lineage is governed. The governed subject is still the previous generation's, so authority-critical paths sit in a trailing position and are refused. All five pass once the carrier lands. A run taken there is not a baseline. |
| Scientific (alone) | 3179 passed / 2 skipped / 0 failed (3181) — science did not move |
| M68D focused (5 suites) | 321 → **333 passed / 2 skipped / 0 failed (335)** |
| Four Windows suites, native | **282 passed / 2 skipped / 0 failed** |
| M68D.1 closure suite | **65 passed / 0 skipped / 0 failed** |
| M68C source integrity | **92 passed / 0 failed** (10 Windows failures → 10 passes, 0 new skips) |
| Four Windows suites, native | 280 passed / 2 skipped / 0 failed |
| Four Windows suites, full simulation | 264 passed / 10 skipped / **1 failed** — the `os.name` artifact of §4, nothing else |
| Mutation campaign | **40/40 detected, 0 survivors, 0 anchor errors** |
| `mutation_campaign_m68c.py` anchors | 52/52 resolve (was 50/52) |
| `mutation_campaign_m68d.py` anchors | 72/72 resolve, unchanged |
| Ruff | PASS |
| Bandit (`-r core tools -ll`) | MEDIUM 0, HIGH 0 |
| `git diff --check` | clean |
| BIDI gate | 24 passed |
| Control Plane | PASS / 0 problems |
