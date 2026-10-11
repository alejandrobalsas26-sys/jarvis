# V69 M68D.3 — Native-aware Windows simulator closure

**Real-CRT behaviour · non-vacuous tests · exact-SHA CI**

Status: **development complete on branch, pending exact-SHA CI qualification.**
Master is untouched. This milestone is not an integration.

---

## 1. Why this milestone exists

M68D.2 sealed generation 70 and repaired two real production defects in
`core/source_integrity.py`. The real Windows runner confirmed the repair: all
six failures from CI 37994281455 were closed.

It then failed for a new reason. The authoritative evidence is **GitHub
Actions run 38007992655**, a `workflow_dispatch` execution against the exact
generation-70 candidate `a0c6f08554ef148791f6ddae126e7b9c1fe5cc3b`:

| Job | Result |
| --- | --- |
| Windows portability (3.11, blocking) | **failure** — 20 failed, 376 passed, 17 skipped (collection 413) |
| Ruff & compileall, release truth, dependency audit, static security, deterministic suite, base install purity, collection containment, M62 control plane, 3.12 smoke, wheel/sdist + manifest scan | success (10/10) |

Windows Control Plane: **PASS / 0 problems.** Windows NEWLINE_POLICY: **PASS.**

**All 20 failures were in the TEST HARNESS, and not one was a production
defect.** They are a disjoint set from the original six.

### The 20 failed test IDs

All in `jarvis/tests/test_cross_platform_source_integrity_m68d2.py::TestBinaryByteIdentity`:

| # | Test | Reported assertion |
| --- | --- | --- |
| 1–8 | `test_every_fixture_survives_a_text_translating_runtime[crlf, mixed_lf_crlf, utf8_unicode, nul_bytes, ctrl_z_early, binary_all_bytes, crlf_on_chunk_boundary, multibyte_on_boundary]` | `the snapshot changed the bytes` |
| 9–16 | `test_stream_mode_digests_the_bytes_on_disk[same eight ids]` | digest mismatch |
| 17 | `test_the_ctrl_z_truncation_is_closed` | `the read stopped at a Ctrl-Z: 26 bytes` |
| 18 | `test_a_crlf_source_is_never_rejected_for_being_crlf` | `a CRLF file was rejected purely for its line endings` |
| 19 | `test_rendering_may_decode_but_only_after_the_bytes_are_fixed` | `b'x = 1\ny = 2\n' == b'x = 1\r\ny = 2\r\n'` |
| 20 | `test_a_cas_precondition_taken_under_translation_still_matches` | `None == 'f8e0f156…'` |

The seven fixtures with nothing to translate (`lf`, `lone_cr`, `cr_at_end`,
`empty`, `bare_lf`, `no_final_newline`) passed, which is itself the tell: the
failures tracked the presence of `\r\n` or `\x1a`, not any property of
production.

---

## 2. Root cause — native CRT versus synthetic shim

The M68D.2 simulator was **unconditional**:

```python
def win_open(path, flags, *args, **kwargs):
    fd = real_open(path, flags & ~WIN_O_BINARY, *args, **kwargs)
    if not flags & WIN_O_BINARY:
        text_fds.add(fd)
    return fd
```

Production derives `O_RDONLY | os.O_BINARY` through its own
`_source_open_flags()` and asks for binary mode — **correctly**, which the
same runner verified via `test_production_asks_the_platform_for_binary_mode`
(it passed). The shim then *removed that bit* and delegated to the real
`os.open`.

On a host with no text mode that is bookkeeping: the stripped bit is one Linux
spends on nothing, and the shim's own `win_read` supplies the translation.

On the real Windows runner the host itself implements the CRT. Stripping
`_O_BINARY` opened an **actual text-mode descriptor**, and the native CRT then:

- collapsed `\r\n` to `\n`, and
- treated `0x1A` (Ctrl-Z) as end-of-file.

`win_read` saw `flags & WIN_O_BINARY` set, so the fd was never marked, so it
passed the bytes through unchanged — already translated by the host. One
translation, applied to the one path that must never be translated.

Measured consequence, byte-for-byte:

| | bytes |
| --- | --- |
| fixture `bytes(range(256)) * 40`, Ctrl-Z at offset 26 | **10240** |
| production's correct binary read | **10240** |
| observed through the M68D.2 shim on a native CRT | **26** |
| `sha256` of that observation | `b858da80d8a5…` |

`b858da80d8a5…` is the digest the real runner printed in *both* CI 37994281455
(where production was genuinely broken) and CI 38007992655 (where only the
simulator was). The simulator reproduced the exact defect it was written to
forbid, from the other direction.

### Independent reproduction, before anything was changed

The operator supplied an explanatory model. It was **not** used as evidence.
The failure was instead reproduced from the repository's own tests, on this
Linux host, by replacing only the **host**: `os.open`/`os.read` were swapped
for a faithful native-CRT model *before* the test module was imported, and the
unmodified repository test functions were then invoked.

Result: **exactly 20 failures, the same 20 test IDs, the same assertion
messages**, and the same `b858da80d8a5…` observation. One root cause for all
20, confirmed rather than assumed.

---

## 3. The central design rule

> **Simulate only what the host does not already provide.**

A simulator must never make a correctly implemented production path behave
incorrectly merely because it replaced a real native operation with a
synthetic one. The production invariant is unchanged:

> When `source_snapshot` requests `O_BINARY`, the observed payload equals the
> physical source bytes.

---

## 4. Measurement methodology

`measure_native_text_runtime()` decides the branch. It never consults
`os.name`, `sys.platform`, `platform.system()`, a filename or a runtime
version — and `TestSkipGovernance::test_this_suite_decides_nothing_from_a_platform_name`
enforces that structurally on the stripped token view.

The probe, in its own `tempfile.mkdtemp()`, isolated from every fixture and
run once at import before anything can be patched:

1. write `b"A\r\nB\r\n\x1aC"` with `Path.write_bytes()`, and **verify it round-trips** —
   a host whose write path translates invalidates everything measured through it;
2. open it with the host's **real** low-level `os.open` using `O_RDONLY` and no
   `O_BINARY`; read it with the host's **real** `os.read`;
3. compare against the exact source bytes.

| observation | verdict |
| --- | --- |
| byte-exact | `BYTE_EXACT` — no native text mode |
| differs, and `O_RDONLY \| os.O_BINARY` **is** exact | `NATIVE_TEXT` |
| differs, and the host publishes no `os.O_BINARY` | `FAILED: …` |
| differs, and the explicit-binary descriptor is **also** inexact | `FAILED: …` |
| `OSError` at any step | `FAILED: <type> errno=<n> winerror=<n>` |

**Fail-closed.** A `FAILED` verdict is neither of the two capabilities, and
`windows_text_runtime()` raises `RuntimeError` on one. An unmeasured host
cannot produce a passing test. The `except OSError` clause is explicit and
records the concrete exception type and errno; it is never resolved into an
optimistic default, which is precisely how the M68D.2 shim came to strip
`O_BINARY` on a host that has one.

`SIMULATED_O_BINARY` — the value injected where the host publishes none — is
**derived**, not assumed: the union of every `O_*` bit the host publishes is
measured, and the lowest free bit at or above `0x8000` wins. On this host the
union is `0x007f7fc3`, so `0x8000` is free and the value is unchanged from
M68D.2. On Windows `WIN_O_BINARY` is the host's **real** `os.O_BINARY`, so the
flag assertions mean the same thing on both kinds of host.

---

## 5. Precise code changes

`core/source_integrity.py` is **byte-identical to generation 70.** Verified by
`git diff`: the only changed files are two test/harness files and one new
campaign script.

### `jarvis/tests/test_cross_platform_source_integrity_m68d2.py`

| Change | What it does |
| --- | --- |
| `measure_native_text_runtime()`, `NativeTextRuntime`, `NATIVE`, `NATIVE_TEXT_TRANSLATION_OBSERVED` | the fail-closed capability probe above |
| `SIMULATED_O_BINARY`, `_free_flag_bit()`, `_HOST_FLAG_BITS` | a derived, non-aliasing synthetic bit (§10) |
| `windows_text_runtime()` | **native branch:** `binary_available=True` is a **no-op** — nothing patched, the host honours production's own flags. `binary_available=False` deletes `os.O_BINARY` so production derives a text-mode flag set and the **real** CRT corrupts the read. No synthetic transform either way, so nothing is modelled twice. **synthetic branch:** unchanged in spirit — one injected attribute, from which production derives its own flags |
| `TextRuntime` | the non-vacuity record: `native`/`synthetic`, `opens`, `text_descriptors`, `translations`, plus `assert_the_branch_is_live()` and `assert_a_translation_happened()` |
| `win_close` / `crt_close` | **a second, independent defect found while building this repair** — see §7 |
| `host_with_a_text_crt()` | replaces the **host** at the layer the probe measures, then **re-measures** and installs whatever the probe says. Lets this Linux host exercise the native branch for real |
| `defective_shim(kind)` | the M68D.2 shim and two neighbours, so the detector has something real to catch |
| `TestTheTextRuntimeReallyTranslates` | renamed from `TestTheSimulatorIsFaithful`; now the PROOF B / PROOF D negative controls, identical assertions on both host kinds |
| `TestTheRuntimeIsNotDoubleSimulated` | 18 tests — §14's absent controls |
| `TestTheSimulatorControlsThemselvesAreIntact` | 8 tests — absent controls for the harness's **own** assertions, scanned **per test function** |
| `TestFailureInjection` + 2 | §17's accounting-fault injection |
| `_test_body()`, `_handler_records_the_failure()` | per-test stripped-view scanning; a narrowed swallow detector |
| mandatory inventory | +5 entries, so the new proofs are **required** to be collected on Windows |

### `jarvis/scripts/mutation_campaign_m68d2.py`

Two anchors re-pointed (the simulator moved), `A_SIM` re-pointed at the renamed
class, and `EXPLAINED_SURVIVORS` emptied — see §8.

### `jarvis/scripts/mutation_campaign_m68d3.py` (new)

28 mutations of the harness itself. Mutates only test files and the workflow;
production mutations stay in the M68D.2 campaign, which still runs.

---

## 6. The four proofs

Each has a non-vacuity assertion, and
`TestTheSimulatorControlsThemselvesAreIntact::test_every_proof_consults_the_branch_liveness_record`
proves structurally that no test can enter a runtime without recording which
branch it got. A test that does not exercise its selected branch does not
count.

| Proof | Host | What it asserts | Measured here |
| --- | --- | --- | --- |
| **A — native positive** | native CRT | the binary source read preserves all bytes | **PASS** on an emulated native host on Linux (`test_the_native_branch_keeps_the_production_read_byte_exact`: 10240 → 10240). **Pending real Windows.** |
| **B — native negative** | native CRT | the unflagged text descriptor exhibits the real runtime behaviour | **PASS** emulated (26 bytes, `b858da80d8a5…`), with `translations == 0 and opens == 0` proving the HOST did it. **Pending real Windows.** |
| **C — simulated positive** | byte-exact | the simulator permits a binary-opened source to preserve exact bytes | **PASS** on this Linux host, all 14 parametrised fixtures, capture and stream mode |
| **D — simulated negative** | byte-exact | the simulator rejects the claim that a text-mode observation is byte-exact | **PASS**, 8 translatable fixtures × 2 modes, with `translations >= 1` |

**Proofs A and B are reported as emulated until the real runner says
otherwise.** An emulated native host is a strong reproduction — it reproduced
all 20 failures exactly — but it is not Windows, and §11 forbids reporting
native proof from simulation.

Negative controls are **not** positive requirements.
`test_no_negative_control_is_taken_with_a_binary_mode_in_force` walks the AST
and fails if any `binary_available=True` block ever asserts that a payload
*differs* from its fixture. The `source_snapshot` path stays binary-exact
unconditionally.

### Byte fixture matrix

All 14 parametrised fixtures are exercised in both capture-bytes and stream
mode, and every binary positive control asserts `payload == fixture`, `size_bytes ==
len(fixture)` and `sha256 == sha256(fixture)`: `lf`, `crlf`, `mixed_lf_crlf`,
`lone_cr`, `cr_at_end`, `empty`, `bare_lf`, `no_final_newline`,
`utf8_unicode`, `nul_bytes`, `ctrl_z_early` (Ctrl-Z at offset 3),
`binary_all_bytes`, `crlf_on_chunk_boundary`, `multibyte_on_boundary` — plus
the 10240-byte `bytes(range(256)) * 40` with its Ctrl-Z at offset 26, which is
staged directly by the two Ctrl-Z tests rather than parametrised. No fixture
was normalised to make a test pass; `digest/size/payload` identity and the
read-to-CAS positive control are all asserted on the original bytes.

**One stated limitation.** `snap.stream()` duplicates the held descriptor and
is read through `io`/`FileIO`, which calls the C `read()` and not `os.read`. A
*synthetic* shim therefore cannot reach the served bytes — on a byte-exact
host the stream-mode negative control asserts only the **digest**, which is
the authority `identify_snapshot` issues as a precondition. The served-bytes
assertion is a real proof of the stream path only on a native host, where the
duplicate inherits the descriptor's actual mode.

---

## 7. Simulator cleanup guarantees

Restoration is asserted on **every** exit: normal completion, assertion
failure, a staged exception, nested contexts and partial setup failure. The
whole mutation now happens *inside* the `try`, so a failure part-way through
setup still restores.

After each test, `os.open`, `os.read` and `os.close` are the originals
(compared against references captured at import), and `os.O_BINARY` has its
original presence **and value**.
`test_the_runtime_restores_itself_through_an_exception` stages a `RuntimeError`
across all four combinations of `binary_available` × host kind;
`test_a_nested_runtime_unwinds_in_order` proves an inner block restores the
**outer** runtime rather than the pristine one. No descriptor leaks and no
simulator state crosses a test boundary, so a failure inside one simulator
test cannot poison unrelated M68C or M68D tests.

### A second defect, found while building this repair

The M68D.2 shim tracked text descriptors in a `set[int]` and never removed a
descriptor when it was **closed**. The kernel reissues the lowest free
descriptor number, so a closed-and-forgotten text fd leaves its number marked
and **the very next open inherits the translation, whatever flags it asked
for**.

This was not theoretical. It fired immediately: the new capability probe's
unflagged read closed fd *N*, the explicit-binary read that followed was given
fd *N* back, and the probe reported *its own binary descriptor* as translating
— `FAILED: the explicitly-binary descriptor is not byte-exact either (4 of 8
bytes)`. On Linux the M68D.2 shim's two reads happened never to collide.

Closed by `win_close`/`crt_close`, which discard the number on close. Pinned
behaviourally by
`test_a_reissued_descriptor_number_does_not_inherit_translation` and
structurally by `test_the_probe_is_still_fail_closed`; mutated by
`F_leak_the_text_descriptor_marking`.

---

## 8. Mutation evidence

### M68D.3 — the harness's own campaign: **28 / 28 detected, 0 survivors, 0 anchor errors**

| Category | Mutations | What they restore |
| --- | --- | --- |
| `D_BRANCH` | 4 | always-enable the simulator on a native host (the defect itself); always-disable it; strip `O_BINARY` on a native host; double-transform a native read |
| `E_PROBE` | 5 | guess from a platform name; turn a probe failure into `BYTE_EXACT`; run on an unmeasured host; skip the binary-descriptor check; stop verifying the probe fixture |
| `F_SHIM` | 7 | remove the Ctrl-Z control; leak the text-descriptor marking; leak patched `os.read`; leak patched `os.open`; never restore `os.O_BINARY`; no restoration after an exception; let the simulated bit alias a host flag |
| `G_VACUITY` | 8 | remove the byte-equality assertion; accept 26 as a complete 10240-byte source; blind the branch-liveness check; let the native branch install a shim; let a negative control translate nothing; let the unrepaired digest match the disk; stop injecting the accounting fault; corrupt the injected digest too |
| `H_CI` | 4 | remove each of three new proofs from the mandatory inventory; remove the M68D.2 suite from the Windows job |

**The campaign falsified the first version of this repair.** Its first run
reported **13/28 detected, 15 survivors** — every one a harness property with
no test behind it, and none of them in production:

- the probe's three fail-closed paths were asserted from a hand-built `FAILED`
  verdict, leaving the code that *produces* one untested → three new
  absent-controls that inject an `OSError`, a broken binary descriptor and a
  translating write path;
- the descriptor-lifecycle fix had no synthetic-branch test → one new test;
- eight assertion-deleting mutations survived because nothing checked the
  assertions existed → `TestTheSimulatorControlsThemselvesAreIntact`;
- four inventory/CI mutations were mapped at the wrong targets → re-mapped.

A second run reported **24/28**, and the four remaining survivors were a
sharper finding: the scans were **file-wide**, and `assert snap.payload() ==
body` appears in half a dozen tests, so deleting the one that carries the
invariant left the scan green. Replaced with **per-test** scans via
`_test_body()`.

One of those four was a real weakness in §17's own test:
`G_accounting_fault_corrupts_the_digest_too` survived because the test
recorded the values the witness *computed* rather than the tuple it *handed
back*. If the injected digest is corrupted too, the digest conjunct does the
rejecting and the length conjunct proves nothing. The test now pins the
returned tuple.

### M68D.2 — the legacy production campaign: **37 / 37 detected, 0 survivors, 0 anchor errors**

Previously 36/37 with one registered survivor.

**`B_drop_the_verified_length` is now DETECTED, and the old explanation was
wrong.** It read: *"the digest comparison strictly implies the length
comparison, so no observable behaviour depends on the length conjunct alone"*.

That does not hold. SHA-256 is a fixed-size mapping over variable-length
inputs, so agreeing digests do not entail agreeing lengths. More decisively,
`verified_sha` and `verified_total` are **not two observations** — they are two
fields of one return value from `_descriptor_content()`, and an accounting
fault inside that helper moves the length while leaving the digest correct.
The M68D.2 comment named exactly that risk ("a future bug in
`_descriptor_content`'s own accounting") and then had no test for it.

`TestFailureInjection::test_an_accounting_fault_in_the_witness_is_refused`
injects it: the witness returns the **right** digest and a length one byte
off. No filesystem bytes are touched and no hash collision is needed. The test
asserts the returned digest **is** correct — so the digest conjunct cannot be
what rejects — and then requires `stable is False`,
`identify_snapshot(...).sha256 is None` and `complete is False`.
`test_an_accounting_fault_is_also_refused_by_recheck` does the same for the
derived-reader path.

Measured with the mutation applied: `stable` goes true and the test fails.
`EXPLAINED_SURVIVORS` is now **empty in both campaigns**. No mutant was
removed or renamed to inflate a count.

---

## 9. Legacy regression results

The Windows job's exact five-suite selection, run on this host:

```
jarvis/tests/test_source_integrity_m68c.py
jarvis/tests/test_trust_boundary_m68d_h02_source_identity.py
jarvis/tests/test_trust_boundary_m68d_h05_portability.py
jarvis/tests/test_windows_portability_closure_m68d1.py
jarvis/tests/test_cross_platform_source_integrity_m68d2.py
→ 456 passed, 2 skipped
```

None removed, none moved behind `xfail`, none made optional. The Windows job
stays **blocking** with no `continue-on-error`, and `H_remove_m68d2_from_the_windows_job`
plus the M68D.2 campaign's `C_windows_job_becomes_advisory` both fail if that
changes. Skip budgets were not broadened: `skip_budget()` and
`DEFAULT_SKIP_FRACTION` are byte-unchanged, and every skip still traces to a
measured primitive.

---

## 10. Preserved unchanged

`core/source_integrity.py` is byte-identical to generation 70, so all of the
following are preserved by construction and by the M68D.2 campaign's 37
mutations: the `source_snapshot` binary-mode flags, `_descriptor_content`'s
byte-level verification, `SourceSnapshot.recheck()`, the metadata-plus-byte
witness, same-size raced-observation rejection, CAS refusal from a detected
unstable source identity, and the derived-reader recheck. H02's
platform-capability tests, the bounded skip-budget contract, NEWLINE_POLICY
and the Git executable-mode invariant are all unchanged and all still run.

---

## 11. Known limitations, carried forward unchanged

### The append defect — still OPEN, still DEFERRED

`cas_write_text(..., mode="a")` may translate LF/CRLF on Windows and
misreport the number of bytes written. Not modified, not certified. The pin in
`TestTheAppendDefectIsStillOpen` is intact and
`C_append_defect_pin_is_blinded` still fails if it is blinded.

**A successful simulator repair does not establish universal cross-platform
write correctness.** It is a statement about the READ path only.

### The M61.7 Bandit suppression detector

The gate matches the substring `nosec`, so an unrelated word containing it
(for example `nanosecond`) turns the gate red, and the obvious narrowing grants
a real suppression. Unchanged, unbroadened, out of scope here.

---

## 12. Production byte-integrity comparison

```
$ git diff --stat a0c6f08..HEAD -- jarvis/core jarvis/tools
(empty)
```

`jarvis/core/` and `jarvis/tools/` are byte-identical to generation 70.
`jarvis/graders/` and `jarvis/training/` are untouched. No training was
performed, no evaluation budget was spent, no scientific state moved.

---

## 13. Exact Windows qualification result

Recorded in the final session report against the sealed generation-71
candidate SHA. **Windows is not qualified until the final mandatory step of
that run completes** — the manifest/secret scan with `--dist` and the import
smoke have never been reached on Windows, because the suite step failed
before them in both previous attempts.
