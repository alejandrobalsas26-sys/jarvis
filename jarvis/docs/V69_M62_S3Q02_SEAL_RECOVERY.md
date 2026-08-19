# V69 M62 S3Q.0.2 — Sealing a measurement that had already happened

**Status:** COMPLETE.
**Subject:** the portable evaluation receipt contract, its schema, its verifiers and the
control plane's acceptance of a post-live seal.
**Not touched:** the measurement. Not one figure, gate message, verdict or digest moved.
**Grants:** nothing.

```
MODEL_LOADS_DURING_RECOVERY         0
MODEL_GENERATIONS_DURING_RECOVERY   0
NEW_EVAL_ATTEMPTS                   0
EVAL_V4_STATUS                      USED_IMMUTABLE (unchanged)
```

---

## 1. The situation this milestone found

The one-shot S3Q held-out evaluation had **already completed**. 36 tasks, 72 model
results, 0 generation errors, canonical eligibility `not_eligible`. `eval-v4` was spent
and could never be spent again. The science was finished.

The **receipt** was not. `m62.eval_receipt.2` had been designed and qualified at S3Q.0.1
against *synthetic* evidence, because the whole point of building it before the
irreversible act was that nobody should design an evidence form while already knowing the
answer. That discipline was right. It also meant `.2` had never once been shown a real
measurement — and when it was, it refused it three times.

Every refusal was `.2` being wrong about production. **The repair moved the contract to
the evidence, never the other way.**

---

## 2. The three findings, each reproduced before anything was written

Reproduced against `.2` as it stood, against the **real** artefacts, in a non-writing
build-to-memory path. A fix asserted without first reproducing the defect passes on a
repository where the defect never existed.

### Finding A — a three-way partition production never had

`.2` required:

```
wins + ties + losses == measured_pairs
```

`comparison.py` classifies **four comparable verdicts**, and the fourth —
`security_improvement`, the baseline having produced a blocking finding the candidate
fixed — is deliberately **not** a win. On the real run:

```
improved 11 + unchanged 12 + regressed 10  =  33
measured_pairs                             =  36
                        security_improvement =  3
```

Observed refusal, from unmodified `.2`:

> `wins, ties and losses do not account for every measured pair`

A correct measurement was being called self-inconsistent by a receipt that had quietly
assumed a vocabulary production never had.

### Finding B — ASCII-only refused legitimate production evidence

`.2` required the canonical receipt text to encode as ASCII, reasoning that otherwise its
bytes depend on an encoding choice. **The reasoning was right and the remedy was
backwards**: it removed the ambiguity by removing the evidence. The real blocking gate
message is

```
schema validity fell from 1.0000 to 0.8889 (−0.1111), past the 0.0500 margin
```

with a **U+2212 MINUS SIGN** that `gates.py` typeset correctly. Observed refusal, from
unmodified `.2`:

> `the receipt is not ASCII, so its canonical bytes depend on an encoding choice`

The report is valid. `.2` refused its representation.

### Finding C — one field cannot hold two sources

`.2`'s `source_identity()` derived `evaluation_source_commit` from `git rev-parse HEAD`
**at receipt-build time**. That is the evaluation source only while sealing happens at the
unchanged evaluated commit — which is to say, right up until sealing fails. Repairing the
receipt requires a commit; a commit moves HEAD.

Reproduced on a throwaway repository with the recovery topology:

```
the evaluation actually ran at       74fb5b27e143…
HEAD after a repair commit           5489c13cff12…
.2 evaluation_source_commit          5489c13cff12…      <- the REPAIR commit
```

and, asserting the truth instead:

> `the caller asserts source commit 74fb5b27e143 and this worktree is at 5489c13cff12`

So `.2` could either **name the repair commit as the thing that measured**, or refuse the
correct statement. `source_identity()` has no field for a seal source at all.

---

## 3. The witness — written first, because it could only be written first

The repository was still standing on `c2c025e…`, the evaluation source, with a clean
worktree. The moment a repair was committed that would stop being true, and nothing in
the repository would be able to say which source state measured.

So before a line of the repair was written, a **pre-repair measurement witness** was built
and committed **alone**:

```
state/m62/witnesses/0001-s3q-live-measurement-witness.json
schema      m62.measurement_witness.1
sha256      00b286a6c44c60c218721fc9d5be1755503306987214aaa88166fd43ed6ef33d
commit      98ff42a334dd42cfbe8d9ae219f461169fcda712
first parent c2c025e720e9c3e595c45ca32bd96bbe974f548e   <- the evaluation source
```

```
evaluation source     c2c025e…
        |                          HEAD here, clean, when the witness was written
        v
measurement witness   98ff42a…     first parent IS the evaluation source
        |
        v
receipt-v3 repair     c9a6aab…     HEAD here when the receipt was built
```

**It is not a receipt.** It grants no candidate state, authorises no retry and promotes
nothing; its `grants` block says so field by field and both the builder and the control
plane refuse a witness that claims otherwise. It binds the evaluation source commit, tree
oid and a re-derivable source digest; the plan, report, manifest, artefact-tree,
comparison and metrics digests; the three ledger events each by its own canonical digest;
the result counts; both partitions; and the eligibility **re-derived** from the report's
own body-free evidence.

The witness builder was promoted into tracked code during the repair and **reproduces the
already-committed witness byte for byte**, so that document is not a one-off artefact of a
script nobody kept.

### What the topology proves, and what it does not

The witness, its Git first parent and the immutable report digests establish **repository
provenance**: which tracked source state this measurement belongs to, fixed before the
repair could move HEAD, and not re-creatable afterwards.

They do **not** prove CPU-level execution authenticity. Nothing here is signed, no PKI is
invented and none is implied. The receipt says so in its own `evidence_level` fields
rather than leaving a reader to assume more than was established.

---

## 4. `m62.eval_receipt.3`

The version moved. `.1` and `.2` are **untouched** — both are tracked contracts whose
documents were written and hashed under their rules, and a semantic rewrite in place would
change what those documents mean. `.2` still builds, still verifies, and stays in the
modern set.

| Finding | What `.3` does |
|---|---|
| **A** | Carries `verdict_counts` over the **exact production vocabulary**, re-derived from `ComparisonVerdict` rather than restated, exhaustive by construction (a zero is written, not omitted), and required to **sum to `measured_pairs`**. `wins/ties/losses` are kept only as the aliases they are — cross-checked against `improved/unchanged/regressed`, declared a **partial** partition, and never summed against the total. |
| **A′** | `numeric_delta_counts` is carried under its **own name** and verified **separately**. The two partitions are never compared bucket for bucket; only the totals must agree. |
| **B** | Canonical bytes are **defined**: canonical JSON encoded UTF-8, stated in the receipt's own `canonical_encoding` field, with `receipt_hash` the SHA-256 over exactly those bytes. No encoding choice remains and no evidence is normalised away to reach that. The token, private-path, body-symbol and task-id scanners are **unchanged**: Unicode is permitted, never privileged. `.1` and `.2` keep their ASCII rule. |
| **C** | `evaluation_source` (bound **through the witness**) and `seal_implementation_source` (HEAD at build, named honestly) are **two blocks**. The conflation is removed by there being two fields — not by choosing which single field to lie in. |

Everything `.2` hardened is carried forward unchanged: mandatory non-empty adapter
identity, the training receipt as the candidate's identity root, exact holdout and pack
identity, policy digests with the generation policy re-derivable from the values beside
it, one plan consumption, one model-facing commit, one recognised terminal event each
bound by its own digest, a single plan hash, result counts, artefact digests, atomic
write, strict standalone verification, and an eligibility **re-derived** by the production
decision algorithm rather than copied.

`.3` also states what sealing did **not** do: `sealed_from_existing_measurement: true`,
`model_loads_during_seal: 0`, `model_generations_during_seal: 0`,
`seal_consumed_no_authority: true`.

---

## 5. The real receipt

```
state/m62/receipts/qwen3-06b-lora-quality-live-003.eval.json
schema                              m62.eval_receipt.3
receipt_hash                        492aae230c3425390a9e32fd81951dff1b22cab42c341c0f509d9b006aaab89c
file sha256                         22f94f15aa101ef339ccf6e611b7b35539acc5bb91a12bca27cad734887c7dff
evaluation_source_commit            c2c025e720e9c3e595c45ca32bd96bbe974f548e
seal_implementation_source_commit   c9a6aab25197bd2dcb110b110cfcc134ef45341a
                                    ^ these DIFFER, and that is the point
measurement_witness_commit          98ff42a334dd42cfbe8d9ae219f461169fcda712
verdict_counts                      improved 11 · unchanged 12 · regressed 10
                                    · security_improvement 3 · security_regression 0
                                    · not_comparable 0        = 36
numeric_delta_counts                positive 13 · zero 13 · negative 10  = 36
non_ascii_codepoints                ["U+2212"]
eligibility                         not_eligible
security_blocking_count             0
status_claim                        EVALUATED_NOT_ELIGIBLE
```

Built from the **existing** gen-1 artefacts, the existing ledger, the tracked training
receipt, the verified runtime adapter and the witness. No evaluation was invoked, no
backend constructed, no model loaded, no token read and no response opened.

It verifies **standalone**: one file, no runtime evaluation directory, no `eval-v4`, no
adapter weights, no model cache.

---

## 6. Non-vacuity

Thirty-six mutations, each applied to an **in-memory throwaway copy** of the real receipt
and **rehashed**, so the question asked is "is this fact checked?" rather than "does the
digest work?". All thirty-six were refused, across the three surfaces a receipt is
actually checked by — the portable verifier, the `.3` seal-recovery bindings and the `.2`
bindings carried forward. The tracked receipt was never written to, and still verifies.

Covered: candidate id · training-receipt digest · adapter SHA / manifest / artefact set ·
evaluation source commit, tree and digest · measurement-witness digest and commit · seal
implementation source · plan hash · report hash · holdout identity · hidden-target-store
hash · ledger event hash · one-ledger-plan-hash · result counts · **each of the six
verdict counts individually** · a verdict shift that keeps the sum · the numeric partition
· decision evidence · decision hash · eligibility · status claim · security blocking count
· **the U+2212 replaced by a hyphen** · extra property · unknown schema · edit without
rehash.

---

## 7. Defects recorded

| Defect | State | What it binds |
|---|---|---|
| **D40** | FIXED at `m62.eval_receipt.3` | A portable receipt may not model the paired outcome as an exhaustive three-way `wins/ties/losses` partition. Production classifies four comparable verdicts and `security_improvement` is not a win. |
| **D41** | FIXED at `m62.eval_receipt.3` | An ASCII-only canonical-text rule is incompatible with legitimate production decision text. Close the encoding question by **defining** the encoding, never by discarding evidence. |
| **D42** | FIXED at `m62.eval_receipt.3` | Deriving the evaluation source from HEAD at receipt-build time conflates the code that **measured** with the code that **built the receipt**, and makes truthful post-live seal recovery impossible. |

D28, D29, D33, D38 and D39 are **unchanged**. Nothing was fixed as a rider.

---

## 8. What this authorises

Nothing. Candidate 003 is `EVALUATED_NOT_ELIGIBLE`. `eval-v4` is `USED_IMMUTABLE`. Any
candidate 004 requires a fresh `eval-v5`, frozen before training begins.
