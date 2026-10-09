# V69 M68D.2 — Cross-platform source integrity

**Binary-exact observation · race truth · Windows falsification**

Status: **development complete on branch, pending exact-SHA CI qualification.**
Master is untouched. This milestone is not an integration.

---

## 1. Why this milestone exists

M68D sealed generation 68 and M68D.1 sealed generation 69. Neither was
integrated, because the mandatory Windows evidence failed both times.

The authoritative evidence for this milestone is **GitHub Actions run
37994281455**, a `workflow_dispatch` execution against the exact
generation-69 candidate `42c0d0c336f1c6a5e6aa730161b5613f36dbfa81`:

| Job | Result |
| --- | --- |
| Windows portability (3.11, blocking) | **failure** — 6 failed, 254 passed, 17 skipped |
| Deterministic suite (3.11, authoritative) | **failure** — 1 test, Docker Hub HTTP 504 |
| Ruff & compileall, control plane, static security, packaging, release truth, 3.12 smoke, base install purity, collection containment | success |

Windows Control Plane: **PASS / 0 problems.** Windows NEWLINE_POLICY: **PASS.**

**Two of the six Windows failures were production integrity defects**, not test
portability. That is the reason this milestone exists, and the reason the
witnesses were repaired rather than suppressed.

### The exact failures

```
FAILED tests/test_trust_boundary_m68d_h02_source_identity.py::TestSnapshotCoherence::test_the_digest_is_over_exactly_the_captured_bytes[line\r\nwindows\r\n]
FAILED tests/test_trust_boundary_m68d_h02_source_identity.py::TestSnapshotCoherence::test_stream_mode_digests_the_same_bytes_it_serves
FAILED tests/test_windows_portability_closure_m68d1.py::TestByteIdentityIsExplicit::test_production_hashing_was_not_taught_to_normalise_newlines
FAILED tests/test_trust_boundary_m68d_h02_source_identity.py::TestSnapshotRaces::test_an_in_place_rewrite_during_the_observation_is_reported_unstable
FAILED tests/test_trust_boundary_m68d_h02_source_identity.py::TestSnapshotRaces::test_an_in_place_race_cannot_state_a_precondition_that_destroys_the_edit
FAILED tests/test_windows_portability_closure_m68d1.py::TestNoSkipWashing::test_most_of_the_h02_suite_still_runs_here
```

Three clusters, each classified independently:

| Group | Count | Classification |
| --- | --- | --- |
| A — text-mode source descriptor | 3 | **CONFIRMED** (production defect) |
| B — metadata-only stability witness | 2 | **CONFIRMED** (production defect) |
| C — unsatisfiable no-skip-washing assertion | 1 | **CONFIRMED** (test contract defect) |

---

## 2. Group A — the source descriptor was opened in text mode

### Root cause

`core/source_integrity.py` opened every source with:

```python
fd = os.open(str(resolved), os.O_RDONLY)
```

Windows' C runtime returns a **text-mode** descriptor unless `_O_BINARY` is
requested. `os.read` on one collapses `\r\n` to `\n` and treats Ctrl-Z (`0x1A`)
as end-of-file. The snapshot then digested whatever came back, so `sha256`
named a file state that never existed on disk.

### Independent reproduction

Linux has no text mode, so the C runtime is **simulated**, driven by exactly
one injected attribute — `os.O_BINARY` — from which production derives its own
flags. Nothing in the harness re-implements that derivation.

Measured, before the repair (`flags = 0x0000`, binary not requested):

| Fixture | On disk | Captured | Digest | `stable` |
| --- | --- | --- | --- | --- |
| LF `a\nb\n` | 4 | 4 | OK | True |
| CRLF `a\r\nb\r\n` | 6 | **4** | **WRONG** | **False** |
| mixed LF/CRLF | 8 | **6** | **WRONG** | **False** |
| lone CR | 4 | 4 | OK | True |
| UTF-8 + CRLF | 11 | **10** | **WRONG** | **False** |
| NUL bytes + CRLF | 5 | **4** | **WRONG** | **False** |
| `bytes(range(256)) * 40` | 10240 | **26** | **WRONG** | **False** |
| CRLF on chunk boundary | 16 | **15** | **WRONG** | **False** |
| multibyte on boundary | 14 | **13** | **WRONG** | **False** |

Seven of ten fixtures violated byte identity. After the repair
(`flags = 0x8000`): **zero.**

The `bytes(range(256)) * 40` row explains the stream-mode failure exactly: the
first `0x1A` is at offset 26, and the read stopped there.

### A second symptom the audit did not name

Unrepaired, `total` came back **shorter than `st_size`** for every CRLF file,
so the metadata witness declared it unstable. On Windows, every CRLF source was
therefore **also denied an identity** — not just given a wrong one. The three
reported Group A failures masked this by asserting the payload first.
`test_a_crlf_source_is_never_rejected_for_being_crlf` is the positive control
that now pins it.

### The fix

```python
_O_BINARY_FLAG = "O_BINARY"

def _source_open_flags() -> int:
    return os.O_RDONLY | getattr(os, _O_BINARY_FLAG, 0)
```

and `fd = os.open(str(resolved), _source_open_flags())`.

Derived per call rather than frozen at import, so a test can exercise
**production's own derivation** from a POSIX host instead of a copy of it. On
POSIX the result is byte-for-byte the previous flag set.

One change at the open repairs the whole path: `os.dup` inherits the
descriptor's mode, so `stream()`, the captured payload, the chunked digest and
every derived parser are fixed at the same point. Nothing was normalised, no
byte seal moved, and `.gitattributes` was not touched.

---

## 3. Group B — metadata-only stability was insufficient

### Root cause

Stability was decided by comparing `st_ino`, `st_dev`, `st_mtime_ns` and
`st_size` across the read. A **same-size in-place rewrite** of the held inode
moves none of the first three, and `st_mtime_ns` carries nanosecond *units*
over roughly **15 ms of resolution** on Windows — so a rewrite landing inside
one clock tick moves nothing at all. A timestamp is evidence, not a version.

### Independent reproduction

Reproduced on Linux without any simulation, by **restoring the mtime** after
the write (`os.utime`) — which is what a coarse filesystem clock does for free.
The result is byte-identical to the real runner's:

```
WRITER_ATTEMPTED  : True        same st_ino      : True
MUTATION_OBSERVED : True        same st_dev      : True
                                same st_size     : True
                                same st_mtime_ns : True

CAPTURED_PAYLOAD  : b'AAAA-veran-wrote-this\n'      <- torn across two versions
STABILITY_VERDICT : True
RECHECK           : True
IDENTITY_SHA256   : bc29f2ad74f53c8e1bedd45f12b0e20ff7d2b78ae811eb85fc7aa9cdd12676cc
IDENTITY_COMPLETE : True
WRITE_PRECONDITION: bc29f2ad74f53c8e1bedd45f12b0e20ff7d2b78ae811eb85fc7aa9cdd12676cc

DIGEST_COHERENT_WITH_PAYLOAD: True
PAYLOAD_IS_ON_DISK          : False
```

That digest is the one CI run 37994281455 reported. The observation was
coherent **with itself** and described a file state that had never existed, and
it was published as a write precondition.

After the repair: `STABILITY_VERDICT: False`, `RECHECK: False`,
`IDENTITY_SHA256: None`, `WRITE_PRECONDITION: None`.

### The four properties, kept apart

This is the part that must not be blurred.

**A · Content-digest coherence.** The returned digest describes the bytes the
authoritative observation returned. **Provided**, and it already was — hashing
the captured bytes establishes this on its own.

**B · Observation stability.** A detectable mutation overlapping acquisition is
not accepted as stable. **Provided, with a stated detection boundary** (below).

**C · CAS safety.** A write derived from an older observation cannot overwrite
changed content because the read-side identity was incoherent. **Provided.**

**D · Transactional snapshot.** A truly atomic, point-in-time image of an
arbitrary file despite hostile concurrent writes. **NOT PROVIDED. This is not a
transactional snapshot.** It cannot be provided with the APIs in use: it
requires cooperating writers, OS-level mandatory locking, an immutable
filesystem snapshot, or another version authority. None of those exists in this
architecture, and inventing an absolute race-freedom claim would be worse than
the defect this milestone repairs.

### The fix: a byte-level witness, alongside the metadata one

```python
def _descriptor_content(fd: int) -> "tuple[str, int]":
    os.lseek(fd, 0, os.SEEK_SET)
    ...  # chunked sha256 of the HELD descriptor
```

Acquisition re-reads the descriptor and compares; `recheck()` does the same
against the published `sha256` and `size_bytes`. Both layers are required:

* The **byte** layer catches a same-size rewrite the metadata layer cannot see.
* The **metadata** layer catches an **ABA** rewrite — write B, restore A — where
  the bytes end up identical and only the timestamp moved. The byte layer is
  blind to that by construction.

Neither subsumes the other. An observation is stable only when both agree.
`test_an_aba_rewrite_that_restores_the_bytes_is_still_reported_unstable` (H02)
is the behavioural proof that the metadata layer is still load-bearing.

The verification pass reads the **held descriptor**, never a reopened path, so
it observes the same inode and an atomic replacement underneath cannot be
mistaken for a mutation of the bytes being read. It is **unconditional** — a
way to switch it off would be a way to obtain an unwitnessed identity.

**Cost:** every observation now makes two passes over the descriptor. For
`capture_bytes=False` that is two full reads of a large file, memory-free but
real I/O. Accepted deliberately; there is no cheaper byte-level witness without
cooperating writers.

### Residual limitations — stated, not engineered away

1. **ABA within one clock tick.** A rewrite that restores both the original
   bytes *and* the original mtime is invisible to both layers.
   `test_the_aba_residual_is_real_and_recorded` asserts this limitation, so the
   documentation cannot drift away from the mechanism.
2. **Mutation after the witness.** The byte pass establishes the state at the
   time it runs. A write landing after it is outside the window, which is
   property D again.
3. **A mutation ahead of the reader.** If the rewrite lands on a region the
   reader has not reached, the reader picks up the new bytes and the capture
   ends up identical to the file's final state. Re-reading agrees because there
   is nothing left to disagree with, so the observation is reported stable.
   **This is the correct outcome**: A, and C still hold exactly — the digest
   describes the bytes returned, and those bytes are the bytes on disk. What
   does not hold is "no writer touched this file while I read it", which is
   property D. Found by this milestone's own suite while it was being written;
   it corrected the author's initial assumption, and
   `test_a_mutation_ahead_of_the_reader_yields_a_coherent_observation` records
   it.
4. **No bound on read size.** Unchanged from M68D; still a later milestone.

What *is* guaranteed, precisely: **a successful observation never returns an
authoritative content/hash pair assembled from different observations, and a
detected instability yields no digest, no `complete`, and no write
precondition.**

---

## 4. Group C — an assertion no result could satisfy

### Root cause

`test_most_of_the_h02_suite_still_runs_here` asserted:

```python
assert passed >= 8 * skipped
```

The H02 matrix is collected **dynamically**: its mechanism parametrisations come
from measured platform capabilities, so Windows collected **55** cases where
this host collects 60, and **9** of the 55 were the expected capability skips.
The assertion therefore demanded **72 passes out of 55 collected cases**.

Arithmetic, not opinion:

```
collected = 55, skipped = 9, failed = 5  ->  passed = 41
assertion requires passed >= 8 * 9 = 72
maximum possible passes with ZERO failures = 55 - 9 = 46
46 < 72  ->  unsatisfiable by any outcome
```

A real Windows runner was blocked by a bar unrelated to the property the
assertion was defending. **The contract was wrong; the security standard was
not.** Nothing was relaxed.

### The fix

`tests/_test_support/suite_census.py` reports what a suite actually did —
passed, failed, skipped, errors, the derived collection, and the **reason text
for every skip**. The replacement contract asserts, directly:

* `failed == 0 and errors == 0` — the thing the ratio was really reaching for;
* `passed + skipped == collected` — a closed accounting of every collected case;
* `skipped <= skip_budget(collected)` — a budget bounded by the **collection**,
  satisfiable at every collection size, and still strictly less than it;
* `passed > 0 and collected > 0` — non-vacuity.

Plus an explicit **mandatory cross-platform invariant inventory**: named node
ids that must be *collected* on every platform — byte identity, Ctrl-Z, CRLF
non-rejection, stream-mode identity, same-size race rejection, derived-reader
race, CAS safety, CAS staleness, Git-mode authority, the NEWLINE_POLICY gate,
and a positive control. A ratio could be satisfied by running any 46 of 55
cases; an inventory can only be satisfied by running *these*.

Every skip must now trace to one of exactly two named, checkable causes: a
**measured** filesystem capability the platform refuses (carrying both the
probe marker and the kernel verdict), or an optional dependency named by
`importorskip`. On this host the H02 suite's two skips are the second kind
(`docx`); on the Windows runner the first kind dominates.

`test_the_old_skip_ratio_is_rejected_as_unsatisfiable` and
`test_the_old_arithmetic_was_unsatisfiable_at_the_measured_collection` are the
non-vacuity proofs that the old form was impossible rather than merely tight.

`passed >= N * skipped` is now **structurally forbidden** across the Windows
suites. It has to be: that assertion *passes* on a host with few capability
skips, so only a scan can catch its reintroduction — which is exactly why the
defect reached a real runner in the first place.

---

## 5. The deferred append defect is still open

`cas_write_text(..., mode="a")` stages an append through a **text** handle. On
Windows `"\n"` becomes `"\r\n"`, so `bytes_written` under-reports what landed
and a caller's `"\r\n"` can become `"\r\r\n"`. Discovered during M68D.1 and
**deferred**: the repair reaches into four places and needs its own
authorisation.

It is **NOT fixed by this milestone**, and must not be recorded as fixed because
the read side now opens binary descriptors. The two paths were checked to be
independent: `test_the_read_side_repair_did_not_touch_the_write_side` asserts
that `_source_open_flags` appears only on the read side, so no shared
dependency was created and no scope broadening was required.
`test_the_append_branch_is_still_a_text_mode_write` pins the defect's shape by
AST, and fails loudly — with instructions — if someone repairs it without
authorisation.

The in-scope half is covered: `mode="w"` is byte-exact on every platform,
including a payload that deliberately mixes `\n` and `\r\n`.

---

## 6. The Linux failure was infrastructure, not code

`test_real_docker_context_excludes_canaries` failed in the same run because
`auth.docker.io/token` returned **HTTP 504 Gateway Timeout**, so image metadata
for `docker.io/library/alpine:3.22` could not be fetched.

This is a **separate infrastructure incident**. The test was not removed, its
assertions were not relaxed, and the failed run is not restated as green. An
outage can *explain* a failed gate; it does not *satisfy* it. The gate must be
observed green on the new exact-SHA qualification run.

---

## 7. Changes to published mutation campaigns

Recorded per M68D.2 §23. No mutation was removed, no detection property was
altered, and no legacy suite was weakened.

* `mutation_campaign_m68d1.py` — the capability probe's own `os.open` now
  requests `O_BINARY`, because it must keep opening the descriptor production
  opens. Two mutations quote that line, in four places (each one's `find` and
  the head of its `replace`); all four were **re-targeted** to the new text.
  The synthetic `os.devnull` line in `B_probe_does_not_hold_the_descriptor`'s
  replacement was left exactly as M68D.1 published it.
* `mutation_campaign_m68c.py`, `mutation_campaign_m68d.py` — unchanged; all
  anchors still resolve.

`test_the_probe_opens_the_path_exactly_as_production_does` pinned both open
sites as literal text and was updated to the new text, with the two constants
hoisted so the pinning is stated once. Its intent is unchanged, and it now also
asserts that probe and production agree about the **flags** rather than only
about using `os.open` — the half that would otherwise drift silently.

M68D.1's declared capability-gate budget is **unchanged at 12**: the new M68D.2
suite adds **no** capability gate, so M68D.1's recorded trade still reads the
way it was measured.

---

## 8. A latent defect this milestone tripped over, and did not fix

`tests/test_bandit_gate_v69_m617.py` builds its suppression inventory with a
bare substring test:

```python
if "nosec" in line
```

A production comment here originally read *"`st_mtime_ns` carries **nanoseco**nd
UNITS over roughly 15 ms of RESOLUTION"* — and `na`**`nosec`**`ond` contains the
letters the detector looks for. The gate reported
`core/source_integrity.py` as carrying a **blanket suppression**, quoting the
comment as the offending line, and two tests went red:

```
FAILED test_the_suppression_inventory_matches_the_allowlist_exactly
FAILED test_no_blanket_suppression_exists_anywhere
  core/source_integrity.py:611 is a blanket suppression:
  '# carries nanosecond UNITS over roughly 15 ms of RESOLUTION on Windows,'
```

**Why this is more than noise.** The obvious way to make the gate green is to
add `core/source_integrity.py` to `ALLOWED_SUPPRESSIONS` — the allowlist that
governs REAL Bandit suppressions. A false positive in this detector therefore
pushes a developer toward granting a suppression allowance to a file that never
asked for one, which is the precise outcome the gate exists to prevent. The
file has **no** `# nosec` of any kind.

**What was done, and what was not.** The comment was reworded to *"carries ns
UNITS"*. That is the whole in-scope repair: `ALLOWED_SUPPRESSIONS` was **not**
touched, no suppression was added, and Bandit stayed at 0 medium / 0 high / 490
low. The detector itself was **NOT** changed. It belongs to M61.7, it is a
security gate this milestone does not own, and tightening it to a word boundary
(`#\s*nosec\b`) is a deliberate decision for its owner rather than a rider on
a portability repair (§17).

**Reported, not closed.** The next comment in `core/` or `tools/` that contains
those six letters will reproduce it. Anyone reading
`source_integrity.py:611` and wondering why it says "ns UNITS" rather than the
natural word will find the answer here.


## 9. Windows-capability rules applied here

Every test in `test_cross_platform_source_integrity_m68d2.py` runs on every
platform; the suite declares **zero** capability gates. Where a mechanism may
be refused — `os.replace` or `os.unlink` over a held path, which Windows denies
with WinError 5 and 32 — the test stages the attempt and asserts the correct
invariant in **both** branches instead of skipping. A refused adversary is
reported `CAPABILITY_UNAVAILABLE` and never counted as adversarial coverage.

Every race carries a witness (`WRITER_ATTEMPTED`, `WRITER_SUCCEEDED`,
`MUTATION_OBSERVED`, writer exceptions, `READER_RESULT`, `STABILITY_VERDICT`)
and asserts the adversary was actually staged **before** asserting anything
about the reader. A writer exception is collected and asserted empty, so a
refused adversary cannot become an invisible pass. No test uses a sleep; every
mutation is invoked from inside `os.read`, so the overlap is guaranteed by
construction rather than sampled.

---

## 10. What this milestone does not claim

* Not a transactional snapshot of an arbitrary file. See §3.
* Not universal Windows compatibility — only the properties listed in §8,
  observed on the runner named in the final report.
* The append byte-accounting defect of §5 remains open.
* No bound on read size.
