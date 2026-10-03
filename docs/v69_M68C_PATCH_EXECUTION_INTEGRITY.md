# V69 M68C — Atomic patch & SWE execution integrity

**Status:** sealed on branch `jarvis-v69-m68c-patch-execution-integrity`. NOT merged.
**Base:** `eb365731776add2bdd21e1555577c8038fd99861` (M68B integrated, generation 65).
**Scope:** the code-modification execution path only. No science, no training, no
corpus, no evaluation, 0 spends.

## 0. The requirement

> JARVIS must never claim that a code change was safely applied unless it can prove
> what it read, what it expected, what it validated, what it actually mutated, and
> what state exists afterward.

Before M68C it could prove none of those five things. The gap was not subtle and it
was not theoretical — every finding below was reproduced on the pristine branch
before a line was changed.

## 1. What the path actually is

M68C's first job was to find the real SWE execution path rather than the one the
audit language implies. There is no patch-application subsystem in this build:

| capability | reality |
|---|---|
| whole-file mutation | `_tool_write_file` → one primitive, one call site |
| source reads | `_tool_read_file` (+ 7 other path-taking handlers) |
| diff production | `_tool_git_query` — `RiskClass.READ_ONLY`, output is a rendering |
| diff consumption | `DiffBudgetGrader` — screens a diff, never applies one |
| **patch application** | **none. No `git apply`, no `patch(1)`, no hunk splicer.** |

That last row is why Finding D is `NOT_REPRODUCIBLE` rather than fixed, and it is
recorded in code as `core.source_integrity.PATCH_APPLICATION_PATHS = ()`. An empty
tuple is a dangerous thing to assert, so the absent-control suite proves its
detector finds an injected `git apply` in a synthetic fixture *before* it reports
that the real tree contains none (§10's non-vacuity rule).

## 2. Findings

### A — source read completeness & identity · CONFIRMED

`_tool_read_file` truncated at `max_chars`, appended the marker
`[...truncado a N chars]` **inside** the content string, and reported `chars` as the
length of the truncated text.

Measured on a 22 890-character file: `chars: 8028`. No `truncated` flag, no digest,
no path — `file` carried only `p.name`. And a **complete** file whose last line
happens to read `[...truncado a 8000 chars]` produced a byte-indistinguishable
result. A caller holding that dict could not tell a whole file from a tenth of one.

**Fix.** `core.source_integrity.identify_source()` returns a `SourceIdentity` with
the canonical path, repo-relative path, existence, size in bytes, a **sha256 over
the complete file**, `digest_covers`, both character counts, and `truncated` /
`complete` as out-of-band booleans. The handler now returns `truncated` and `source`
alongside the unchanged legacy keys.

The load-bearing split: **the digest always covers the complete file, even when the
rendering was cut.** Truncating a display therefore never costs a caller its ability
to state a write precondition, and never lets it claim it saw the whole file.
`truncated` is *derived* from the two counts, so it cannot be declared away.

### B — atomic write + optimistic concurrency · CONFIRMED

`_tool_write_file` was `open(p, mode, encoding="utf-8")`. No precondition, no temp
file, no post-state check.

Measured lost update: JARVIS read `VERSION = 1`; a human changed the file to
`VERSION = 2`; JARVIS wrote its edit; the human's line was gone, under
`{"written": ..., "bytes": 13}`.

**Fix.** `cas_write_text()`:

* `expected_sha256` — supply the `source.sha256` a read returned and the write
  happens only if the file is still that version. The literal `"ABSENT"` requires
  that no file exists. Omitted, last-writer-wins is preserved, so the parameter is
  purely additive; **supplied, it is enforced**.
* whole-file writes stage into a temp file **in the target's own directory**, fsync,
  then `os.replace`.
* the comparison, the replace and the post-state verification run inside **one
  advisory `flock` on the target's directory**.

#### Why the lock exists (a defect M68C found in its own first fix)

The first implementation had the precondition and the atomic replace but no lock.
A two-thread barrier test then produced **two `APPLIED` receipts over one surviving
file**: both writers passed the digest comparison, both replaced, and each one's
post-state check ran before the other's replace. One of those receipts described a
state that no longer existed — exactly the lie this milestone exists to prevent.

`flock` is taken on the **directory's own descriptor**, not a lock file: nothing to
leak, nothing to clean up after a crash, no race over unlinking a lock another
waiter already holds. On a host without `fcntl` (Windows) the lock is a no-op and
`WriteReceipt.serialised` is `False`, so the receipt states plainly that
simultaneous writers were not serialised instead of implying they were.

### C — diff / patch transport completeness · CONFIRMED

`_tool_git_query` cut `stdout` at 3 000 characters with **no marker at all** —
worse than Finding A, which at least had an in-band one.

Measured: 31 876 characters of `git log` returned as exactly 3 000, with nothing in
the result saying so.

**Fix.** `identify_transport()` returns a `TransportIdentity` with total and retained
byte counts, `truncated`, a **sha256 over the complete output**, the producer, and an
`artifact_class` of `COMPLETE_PATCH` or `TRUNCATED_DISPLAY_ONLY`.
`usable_as_patch_artifact` is the explicit answer to "is this a patch?" and is
`False` for anything cut.

Hashing the *retained* bytes would make a truncated rendering self-consistent and
therefore undetectable; the test suite pins the digest to the complete output and a
campaign mutation proves that assertion is load-bearing.

> Note on units: the cap is 3 000 **characters** and the counts are **bytes**. On this
> repository's own `git log` that is 3 000 chars == 3 056 bytes, because commit
> subjects use em-dashes. Conflating the two is how a boundary check passes on ASCII
> and lies on everything else.

### D — patch applicability / safety preflight · NOT_REPRODUCIBLE (as written)

There is no patch-application path to bypass an applicability gate. What the audit
concern maps onto in *this* build was addressed:

* **anti-TOCTOU.** The precondition is not a preflight that returns and is followed
  later by a mutation. It is read inside `_write_locked`, under the lock, with no I/O
  between the comparison and the `os.replace` except writing a file that is not the
  target. An absent-control test asserts that source ordering.
* **path containment.** `_resolve_within_allowed` remains on the mutation path
  (M66A.1's single gate) — and is now verified **behaviourally**, not just by its
  presence. See the campaign note below.
* **multi-file atomicity.** Not claimed, because nothing applies multi-file patches.
  One call mutates one path.

### E — truthful patch receipts · CONFIRMED

The old success shape was `{"written": ..., "bytes": ..., "mode": ...}`: no before
state, no after state, no way to express "validated but not applied", "refused as
stale", or "the boundary was crossed and the outcome is unknown".

**Fix.** `WriteReceipt` carries an operation id, the status, both file identities,
the precondition verbatim, the intended digest, the mutation method, and the
atomicity/serialisation/durability claims. `WriteStatus` separates `APPLIED`,
`VALIDATED_NOT_APPLIED`, `REJECTED_STALE`, `REJECTED_INVALID`, `REJECTED_POLICY`,
`FAILED_BEFORE_MUTATION` and `PARTIAL_OR_UNKNOWN`.

**No parallel truth model.** Each status maps onto M65D's existing
`core.effect_journal.ExternalOutcome` — `PROVEN_COMMITTED`, `PROVEN_NOT_EXECUTED`,
`UNKNOWN` — through `_WRITE_STATUS_OUTCOME`, and a test asserts that mapping is
total and that `APPLIED` is the only status reaching `PROVEN_COMMITTED`.

`APPLIED` requires post-state evidence: the file is re-identified after the replace
and compared against the digest of the bytes that were supposed to land. A mismatch
is `PARTIAL_OR_UNKNOWN`, never success. Receipts never store bodies — the digests
are the proof, and keeping the text would make every receipt a copy of the file it
describes.

The effect journal and CAS compose without overlapping: the journal answers "did
this already happen?", CAS answers "is the file still the version I based this edit
on?".

## 3. Grader truth (§9), and a freeze M68C broke once

`DiffBudgetGrader` never claimed applicability and its docstring has always stated
its limits. M68C's job was to make the scope **machine-readable** so a rename, a
future reader or a caller in a hurry cannot blur it.

**The first attempt did that wrongly.** It added `VALIDATION_SCOPE = "SYNTAX_ONLY"`
as a class attribute on the grader — and `jarvis/training_gym/graders/` is FROZEN
scientific machinery, byte-identical since `05c043b3`, enforced by
`test_the_graders_and_the_refusal_detector_are_untouched`. That test failed in the
full authoritative suite, 12 589 tests in. A preregistered grader may not be edited
to make a later milestone tidier, however benign the edit looks, and "it only adds
a constant" is not an exemption — the freeze is what makes every earlier
measurement re-derivable.

**The fix** is `core.source_integrity.PATCH_VALIDATION_SCOPES`, a registry mapping
dotted import paths to a `PatchValidationScope`. It is arguably the better shape
anyway: one place names what every diff-consuming component proves, instead of each
component asserting it about itself. Guards:

* every registry key is **resolved** by the absent-control suite, so a typo cannot
  make the checks pass over nothing;
* an AST sweep asserts **no component self-declares** a competing `VALIDATION_SCOPE`
  — including the grader, so the broken edit cannot come back;
* nothing maps to `APPLICABILITY_PROVEN`;
* `FileDiff` exposes no `applies`/`applicable` field that would make a parse result
  look like an apply check at every call site;
* a **millisecond** guard re-asserts the freeze inside the M68C suite, because
  learning about it 12 000 tests later is a bad place to find out.

The worked example is a test: a well-formed diff whose context line does not exist
in the tree it names parses cleanly and is still not applicable.

**Residual, and it is a real one.** Because the grader tree is frozen, the campaign
**never writes to it**, even transiently — a crash between mutate and restore would
leave preregistered machinery modified. So the grader's own internal controls are
verified behaviourally, not by falsification, and `TestGraderIsSyntaxOnly`
deliberately has **no mutation mapped to it**. That gap is recorded here rather
than papered over with a mutation that targets something else and claims the credit.

## 4. The campaign found six defects in M68C's own work

52 mutations, one at a time, each mapped test green unmutated first, fresh process,
source always restored. The first run reported **12 survivors**. None was excused;
each is recorded here because a survivor is evidence.

**Real test gaps (6):**

1. `complete` dropped its digest requirement undetected — only `exists` was pinned.
   Both halves are now independently asserted on a constructed identity.
2. The digest covered only its first chunk undetected — every test file was shorter
   than one read. Fixed with a long-shared-prefix pair and a multi-chunk file.
3. / 4. `"source": {}` and `"transport": {}` satisfied key-presence probes. The
   payload **contents** are now asserted.
5. / 6. `to_dict()` could drop `before`/`after` while the dataclass attributes stayed
   intact. The **serialised** form is now asserted — it is what a caller receives.

**A detector that only caught the spelling it was written from (1):**

`B_handler_bypasses_the_primitive` reintroduced the raw write as
`open(p, mode, encoding="utf-8")` and survived, because the scanner read the
**non-literal** `mode` as `""` and matched nothing. The scanner now **fails closed**
on any `open()` whose mode it cannot prove read-only.

**A structural test that proved a call site, not a boundary (1):**

`D_handler_skips_containment` kept `_resolve_within_allowed(path)` and defeated it
with `... or Path(path).expanduser()`. The AST probe saw the call and passed. Only
observing the refusal catches that, so a behavioural test now writes to `/etc/...`,
`../../../../etc/...` and `~/../../etc/...` and requires `PATH_NOT_ALLOWED`.

**A control asserted in the wrong module (1):**

`B_direct_open_instead_of_replace` swapped temp-and-replace for
`open(target, "wb")` and survived the handler sweep, because that sweep only scans
`_tool_*` in `executor.py` — the raw write had moved one module down. Atomicity is
now asserted where it lives.

**Four mutations were themselves invalid, and are documented rather than deleted:**

* `D_grader_scope_removed` anchored on the **comment** above the attribute, so it
  deleted prose and left `VALIDATION_SCOPE` intact. It survived because it changed
  nothing. Anchor corrected to the attribute.
* `E_receipt_leaks_the_body` added `reason` to the dict — metadata, and `None` on the
  success path. It leaked nothing. Corrected to carry the actual content.
* `E_claims_serialised_always` mutated the **dataclass default**, which no code path
  reads (`_receipt` always passes the keyword). Semantically inert. Corrected to the
  `_receipt` keyword default, which every pre-lock return does read.
* `D_registry_emptied` was `= {} or {...}`, which evaluates to the **second** dict
  because `{}` is falsy — the registry was never emptied. Corrected to anchor the
  whole literal.

**Final: 52 mutations, 52 detected, 0 survivors, 0 anchor errors.**

## 5. Atomicity claim discipline (§17)

What is claimed:

* **atomic visibility** for whole-file writes. `os.replace` is atomic on POSIX and
  Windows; no observer sees a partial file.
* **the original survives a failed replace**, because the target is never opened for
  writing on the whole-file path. Tested, including with an exploding `os.replace`.
* **serialisation against other writers going through this primitive**, where
  `fcntl` exists, stated per-receipt.
* **no leaked locks or descriptors.** The lock is a directory fd released in a
  `finally`; 200 writes and 200 refusals each leave the descriptor count unchanged.
  A missing `os.close` would fail under load rather than on the first call, so the
  campaign carries a mutation that removes it.

What is **not** claimed:

* **crash durability.** The temp file and its directory are fsynced, which is the
  right thing to do and is not evidence. No host has been power-cycled.
  `crash_durability_proven` is `False` and no caller may upgrade it.
* **cross-file transactionality.** One call, one path.
* **rollback.** Once a replace succeeds the previous content is gone; only its digest
  survives in the receipt.
* **append atomicity.** An append is declared `MutationMethod.APPEND` with
  `atomic_visibility=False`. It cannot be made atomic and is not dressed up as such.

## 6. Residual limitations

1. **The advisory lock binds only writers that go through `cas_write_text`.** A
   foreign process writing the same path is a race no local lock can win. Every
   mutation path JARVIS has does go through it, and the absent-control suite is what
   keeps that true.
2. **No `fcntl`, no serialisation.** On Windows `serialised` is `False` and the
   compare→replace window is real. The receipt says so rather than implying
   otherwise.
3. **Crash durability is unproven**, as above.
4. **`identify_source` on a derived-text file digests the FILE, not the text.** For a
   PDF or an OCR'd image, "I read this" and "this is what the file contains" are
   different claims; `content_derived` records which was made.
5. **CAS is opt-in.** A caller that supplies no precondition still gets
   last-writer-wins. This preserves every existing caller's behaviour; the tool
   schema now instructs the model to pass `expected_sha256` whenever it edits a file
   it read.

## 7. Explicitly out of scope and untouched

M68B's AURA architecture, `code_intel` resource model, Docker persistence, bounded
output and Ollama resolver; the `tools/executor.py` VLM screenshot Ollama hardcode;
Terminal-Bench, DeepSWE, OSWorld, SWE-bench; model training, reasoning adaptation,
corpus ingestion, candidate006, eval-v8, promotion. No dependency changes.
