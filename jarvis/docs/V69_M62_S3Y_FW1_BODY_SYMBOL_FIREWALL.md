# V69 M62 S3Y.FW1 — the live-holdout body-symbol firewall reaches eval-v6

**Security hardening only.** No generation transition, no evaluation, no spend. The
control plane stays at **generation 13**.

---

## 1. What was found

S3Y completed its pre-authorisation with **zero evaluation and zero spend**. During that
work two separate facts came to light:

1. Real `eval-v6` task identifiers are live-exam information and were **surfaced** in the
   pre-authorisation session. No `eval-v6` prompt, target or task body was surfaced. The
   task-id firewall that governs this is a **separate** subsystem, it already existed, and
   it is unchanged here — it was re-run and it passes.
2. A distinct omission in the **body-symbol registry**: `FORBIDDEN_BODY_SYMBOLS` in
   `jarvis/scripts/verify_m62_control_plane.py` named the eval-v4 and eval-v5 corpus
   symbols but not eval-v6's. A scanned control-plane surface could therefore cite the
   live generation's body-bearing builder without refusal. The live exam was guarded by
   the task-id scan and the 320-character field cap, not by a symbol scan.

S3Y recorded (2) deliberately as an open gap rather than patching it inside the single-use
ceremony it exists to protect. This milestone is that patch, and nothing else.

## 2. What changed

Each corpus generation is reachable through **two** surfaces: the function holding the
material, and the wrapper returning it. Every historical generation therefore occupies two
registry slots, and protecting only one of a pair leaves a working citation. Both eval-v6
slots were appended.

The registry carries a standing contract — *appended to, never reordered* — because sealed
suites index it positionally. The change is a strict **append**: the historical prefix
keeps its entries, its order and its indices, and the only delta is a two-entry suffix.
The scanner algorithm, its scan surfaces, its containment semantics, the H4 policy, the
verifier's self-exclusion and all task-id derivation are untouched.

This document names the two new entries by role rather than by literal, for the same
reason the registry exists.

## 3. What proves it

`jarvis/tests/test_training_gym_m62_s3y_fw1_body_symbol_firewall.py`:

* the historical prefix is still the historical prefix, in order, with slot 0 unmoved;
* the appended suffix is exactly the two eval-v6 entries, and the registry grew by nothing
  else;
* a **synthetic** scanned surface citing the material symbol is refused;
* a **synthetic** scanned surface citing the wrapper symbol is refused — asserted
  separately, because material-only protection is incomplete;
* **non-vacuity**: removing either new entry, in memory, silences its own probe while
  leaving the other one working, so neither can be inferred from the other. Reverting the
  registry outright fails both positive probes.
* **negative control** at the scanner's own containment semantics, recorded rather than
  redesigned.

Every probe string is synthetic. The suite never opens the body-bearing builder — not to
read it and not to parse it — imports no corpus builder, materialises no pack and
enumerates no real evaluation task identifier. It asserts its own freedom from authority
tokens and private paths with the control plane's own patterns. Whether any tracked file
names a real held-out identifier stays the sole responsibility of the separate, canonical
task-id firewall, which is unchanged here: a registry regression that re-asked that
question would have to iterate the identifiers to search for them.

## 4. Scientific state — unchanged

| Fact | Value |
|---|---|
| Control plane generation | 13 |
| candidate 004 | `TRAINED_UNEVALUATED`, `evaluation_corpus` null, `evaluation_receipt` null |
| `eval-v6` | `FROZEN_UNUSED`, `spent_by` null |
| `eval-v5` | `FROZEN_UNUSED`, `spent_by` null, RETIRED from eligibility use |
| Model weight loads / generations / evaluation attempts / holdout spends | 0 / 0 / 0 / 0 |
| EVAL authority / promotion authority | none |

Candidate 004 remains **unmeasured**. `eval-v6` remains **unspent**.

## 5. Consequence for S3Y

This milestone changes tracked repository code, so every pre-repair S3Y plan and
confirmation is **stale**, including any that would recompute to identical material. No
pre-repair authority may be reused.

A live evaluation requires a **fresh session** from this commit: a new canonical
`--live-preflight`, a new plan hash, and a new explicit human GO.

## 6. Note for the operator, not repaired here

The S3Y capacity table records `DURABILITY_FAILURE` at 33 871 bytes. The projector
measures **33 789**, which is what its own headroom column already says: 34 816 − 1 027 =
33 789. The byte cell is a transcription slip; the headroom, the worst-case figure and the
PASS verdict are unaffected. It measures identically before and after this patch, so it is
**pre-existing and not caused by this change**, and S3Y's record is left as written.
