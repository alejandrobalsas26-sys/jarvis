# V69 M62 S3Y — candidate 004 against eval-v6: qualification and pre-authorisation

**Status at the time of writing: PRE-AUTHORISATION COMPLETE, NOTHING MEASURED.**

Zero model weight loads. Zero generations. Zero evaluation attempts. Zero holdout spend
events. Zero EVAL authority created, requested or consumed. `eval-v6` is `FROZEN_UNUSED`
with `spent_by` null. Candidate 004 is `TRAINED_UNEVALUATED` with both evaluation fields
null.

This document records everything S3Y did **before** the human authorisation barrier, and
is the evidence surface generation 14 will point at once — and only if — an operator
supplies the canonical `EVAL:<plan-hash>` token for the plan named in §10.

**No held-out task body appears anywhere in this document.** Every identity below is a
digest, a count, a split name, a family name or a task-kind name.

---

## 1. Start-state recovery — verified, not assumed

This is a fresh session. It did not author `eval-v6`. Nothing was taken from
conversational memory; every fact below was re-derived from Git, the repository, the
Control Plane and this execution host.

| Surface | Expected | Measured | |
|---|---|---|---|
| branch | `jarvis-v69-m62-training-gym` | same | ✓ |
| HEAD | `c250054acc27723a842d2087de8a66d1c9a467b9` | same | ✓ |
| `origin/<branch>` | equal to HEAD | equal | ✓ |
| divergence | `0 0` | `0 0` | ✓ |
| master | `3705114228edef2f665be349c5c4429b7b16777a` | same, and `origin/master` agrees | ✓ |
| worktree | CLEAN | CLEAN | ✓ |
| generation | 13 | 13 | ✓ |
| current pointer | `state/m62/snapshots/0013-m62-s3x1-fresh-eval-v6-frozen.json` | same | ✓ |
| snapshot SHA-256 | `9f49c759b32c571b05b285be9da210da6a609c0aaea6e059010b07bcf2dc6f6c` | same | ✓ |
| verifier | PASS / PROBLEMS 0 | PASS / PROBLEMS 0, all 15 sections PASS | ✓ |
| D44 | FIXED, `is_gate: true` | FIXED, `is_gate: true` | ✓ |
| authority observation | EVAL NONE, PROMOTION NONE | both `NONE_OBSERVED_IN_REPOSITORY` | ✓ |

### 1.1 The generation-13 byte size, settled

Historical reporting carried two incompatible figures: **33 783 / 1 033 headroom** and
**33 788 / 1 028 headroom**. Neither was trusted. The artefact on disk was measured
instead, and it is authoritative:

```
GEN13_CANONICAL_BYTES:  33788
GEN13_HEADROOM:         1028          (34816 - 33788)
```

The file is **byte-identical to its own canonical re-serialisation**
(`canonical_bytes(json.loads(raw)) == raw`), and its SHA-256 equals the digest
`current.json` records, so there is no third answer available. **33 788 / 1 028 is
correct; 33 783 / 1 033 is not.** The superseded prose is not rewritten — the correction
is recorded here, prospectively, in line with the rule that a superseded snapshot is never
revised.

### 1.2 Candidate 004, re-derived body-free

| Field | Value |
|---|---|
| `candidate_id` | `qwen3-06b-lora-quality-live-004` |
| status | `TRAINED_UNEVALUATED` |
| `evaluation_corpus` | `null` |
| `evaluation_receipt` | `null` |
| adapter SHA-256 | `a105e01ca99d9b47d45c408a614b78aa9ec22df83ad32b321df57b1a1c3ecc67` |
| adapter manifest | `162e93e36f284b651051a93e22cfc6cb15adef3f457038297ca72774e276b510` |
| base revision | `c1899de289a04d12100db370d81485cdf75e47ca` (immutable commit) |
| training corpus | `m62-defensive-quality-train v2` |
| training receipt | `state/m62/receipts/qwen3-06b-lora-quality-live-004.train.json` |

The adapter file on disk hashes to the SHA-256 the snapshot and the training receipt both
record: **no post-training mutation.** The training receipt independently reports
`eval_authority_created: false`, `evaluation_corpus: null`,
`held_out_evaluation_runs: 0` and `model_response_tokens_generated: 0`, and its TRAIN
authority is spent and non-reusable (`creations: 1`, `consumptions: 1`,
`retry_authorized: false`).

`state/m62/receipts/` holds no `qwen3-06b-lora-quality-live-004.eval.json`. No candidate
004 evaluation receipt has ever existed.

### 1.3 The holdouts

`eval-v6` — `FROZEN_UNUSED`, `spent_by` null, 36 tasks, manifest
`413e675711d51f5b98cb5a8ec7ff7fb0d8eb36b5e4c6dff790fb60f764f8fba6`, pack
`41579381422636d073d8ce3a0df230cafb97ffdd1489ab02126f2273565ade16`, declared parent
`e852f462…` (eval-v5). The sole fresh eligibility corpus.

`eval-v5` — `FROZEN_UNUSED`, `spent_by` null, **RETIRED FROM ELIGIBILITY USE**. Untouched.

`eval-v4` — `USED_IMMUTABLE`, spent by S3Q on candidate 003.

**No S3Y evaluation run, receipt or spend exists, and no post-S3X.1 candidate-004
model-facing activity was found.**

---

## 2. Holdout firewall — one incident, disclosed

The orchestration session must never receive an `eval-v6` body. It did not. But this
section records a real incident rather than a clean claim, because a firewall milestone
that reports only its successes is the one that produced D44.

### 2.1 What happened

This repository stores held-out task bodies as **literal Python string literals in a
tracked source file**, `jarvis/scripts/build_evaluation_corpus.py`
(`corpus_v1_material()` … `corpus_v6_material()`). That is not obvious from the Control
Plane, which describes datasets only by digest, and the versioned dataset directories for
v5 and v6 do not exist on this host — so a keyword search was used to locate where `v6`
lives.

That search matched inside the generator and printed roughly six partial lines into the
session. They were fragments of **eval-v5 and eval-v1–v4** prompt and target text.

### 2.2 What did NOT happen

**No `eval-v6` prompt, target or task body reached the orchestration session.** The
`S3Y_HOLDOUT_FIREWALL_BREACH` condition is defined as a v6 **body** reaching the
orchestrator, and it was not met.

But the claim must be narrower than "v6 was untouched". The same matched lines were tuple
headers carrying split, family and **task id**, and a handful of v6 task ids were displayed.
This repository does **not** treat those as harmless: `verify_m62_control_plane` reconstructs
all three held-out id sets precisely so no surface may name one, and calls the v6 set "the
most load-bearing of the three ... a surface naming one of its tasks leaks the live exam".

So the honest statement is: **no v6 body leaked; a small number of v6 task identifiers did.**
An identifier discloses that a task exists in a family and split whose counts were already
public; it discloses no prompt, no target and no answer. The exam's content is intact and
its measurement value is undamaged. It is still a disclosure, and it is recorded as one.

### 2.3 Assessment, stated plainly

The material exposed was `eval-v5`, already **permanently exposed and retired unspent**
under D44, and `eval-v4`, already **spent**. Neither has any remaining eligibility value,
so the incremental scientific harm is nil. It is nonetheless a violation of the standing
instruction not to re-open eval-v5, and it is recorded as one rather than absorbed.

`eval-v6`'s freshness is intact, and the ceremony's measurement value is undamaged.

### 2.4 Containment

* `jarvis/scripts/build_evaluation_corpus.py` was placed under a read-lockdown for the
  remainder of the session: only `grep -l`, `grep -c` and patterns that can match code
  structure alone (`^def `, `add_argument`, `^[A-Z_]* =`). No keyword greps, no context
  flags, no file reads.
* The same lockdown, and the reason for it, was sent to the reconnaissance agent working
  in parallel **before** it reached that file. It reported its work firewall-clean.
* Every subsequent operation that had to touch held-out bytes was run through a wrapper
  that prints an allow-listed set of body-free keys and reports exceptions **by type
  only** (§4.2).

### 2.5 The D44 regression gate

```
tests/test_training_gym_m62_s3x0_repr_body_blindness.py    32 passed
D44                                    FIXED
POST_FIX_CANARY_LEAKS                  0
BODY_BLINDNESS_NONVACUITY              PASS   (3 guard-removed probes must leak)
SEMANTIC_EVALUATION_IDENTITIES_CHANGED NO
```

Synthetic canaries only. No real v6 material was used as a canary.

---

## 3. Generation-14 capacity — proved before the spend

Generation 13 closed with **1 028 bytes** of headroom against a policy floor of **1 024**.
Four bytes of slack, and generation 14 must carry strictly more truth. The S3Y contract
bars every escape: no budget raise, no headroom-floor reduction, no weakening of the
320-character field firewall, no deletion of defects, limitations or invariants.

A new projector, `jarvis/scripts/project_m62_gen14_capacity.py`, projects **four** truthful
terminal states from the real generation-13 snapshot through the same `canonical_bytes`
the emitter and verifier use, so the bytes measured are the bytes that would land.

> **Superseded by S3Y.CAP1.** The figures first recorded here were measured against a
> `test_baseline` stand-in of the wrong shape and are corrected below. The original table
> also carried a transposition: it reported `DURABILITY_FAILURE` at 33 871 bytes beside
> 1 027 headroom, which cannot both hold, since 34 816 − 1 027 = 33 **789**. The headroom
> column was right and the byte column was wrong.

**The first proof was not conservative.** The projector's CLI and its sealed test helper
both defaulted `passed`/`skipped`/`failed` to `0` — one digit each — while a real baseline
carries a four-digit pass count and a two-digit skip count. Truthful values therefore add
**four bytes** to every projection, and four bytes was the entire margin: the worst-case
ending measured 1 027 spare against a 1 024 floor, so the truthful figure was **1 023** and
the gate was green only because it was measuring a snapshot that could never be written.

S3Y.CAP1 replaces the stand-ins with the RIGHT-SIZED bounded maxima
(`STANDIN_PASSED` 99 999, `STANDIN_SKIPPED` 999, `STANDIN_FAILED` 999), refuses any real
baseline wider than the width capacity was proved against, and refuses a `spent_by` field
longer than the projection assumed — a full 64-hex digest in place of the 8-character short
form would have added 112 bytes unnoticed. The evaluation generation ordinal is now
interpolated rather than hard-coded as `gen-1`.

Room was recovered by **two authorised rewrites only** — no budget raise, no floor
reduction, no deletion, and `check_carried_forward` returns empty in all four states:

* **kind (c)** — the limitation naming the holdout-author disqualification stopped
  restating the procedural caveat that generation 13 had already moved onto
  `frozen_invariants`. The rule stays on the invariant surface; the limitation keeps the
  candidate-004 instance. −80 bytes.
* **kind (a)** — `GEN11 IS READINESS, NOT AUTHORITY` asserted "no human authorised
  evaluation, no holdout is spent". Generation 14 falsifies both in **all four** endings,
  because the spend boundary is the durable commit and not proof a forward pass finished.
  Shipped unchanged it would have emitted a snapshot contradicting its own `spent_by`.
  Rewritten to the settled fact, it is 29 bytes shorter and, more importantly, **true**.

| Terminal state | Bytes | Headroom | |
|---|---:|---:|---|
| `ELIGIBLE` | 33 677 | **1 139** | PASS |
| `NOT_ELIGIBLE` | 33 646 | **1 170** | PASS |
| `ABORTED` | 33 409 | **1 407** | PASS |
| `DURABILITY_FAILURE` | 33 684 | **1 132** | PASS |

```
SNAPSHOT_MAX_BYTES        34816   (unchanged)
REQUIRED_HEADROOM_BYTES    1024   (unchanged)
GEN14_WORST_CASE_HEADROOM  1132   (truthful baseline 4581/20/0)
GEN14_WORST_CASE_HEADROOM  1128   (right-sized stand-in 99999/999/999 -- the gate)
S3Y_GEN14_CAPACITY         PASS
PROGRESS_BYTES  40695  HEADROOM 265   LINES 610  HEADROOM 150
```

The gate is now decided on the **stand-in**, which bounds every truthful baseline rather
than undercutting it, and it clears the floor by 104 bytes instead of by a fiction.

### 3.1 Why the failure states are projected at all

An evaluation has more than one truthful ending and the gate is decided on the **largest**,
not the most convenient:

* **`ABORTED`** — the runtime crosses the model-facing boundary and stops. `eval-v6` is
  **still spent**: the spend boundary is the durable `holdout_model_facing_committed`
  event, deliberately earlier than proof a forward pass finished. With no portable receipt
  candidate 004 may not claim an `EVALUATED_*` state, so it stays `TRAINED_UNEVALUATED`
  beside a spent holdout — an ugly state, and the true one.
* **`DURABILITY_FAILURE`** — a valid measurement whose durable evidence does not land. Not
  a clean success; recovery, never a rerun. It adds a defect entry and is the worst case.

The first projection did **not** fit: `DURABILITY_FAILURE` came out 275 bytes **over**
budget, i.e. 1 299 short of the floor. It fits now only because of compaction, and only
compaction of three permitted kinds.

### 3.2 What was compacted, and why each is lossless

1. **`next_milestone`** — prospective by construction. Generation 13's described S3Y, which
   by the time generation 14 is written has happened. Its replacement is written tight
   because the permanent rules it used to restate at length live on `frozen_invariants`,
   which is where permanent rules belong.
2. **Clauses generation 14 settles**, rewritten to the settled fact: the spend boundary
   stopped being "prospective"; "no model has been generated under the D38 instrument" and
   "nothing was loaded, only identified" are what S3Y is; "eval-v6 next" became "eval-v6".
3. **Two entries that already carried one subject, merged** — candidate 004's two
   single-axis caveats; the never-enforced timeout, which D28/D29/D33 already carried and
   the S3Q.0 surface note repeated.

Three additional duplications were removed because generation 13 had already moved the rule
onto the invariant surface: the holdout-author firewall, the retention-and-recovery rule
(restated by both the D45 defect summary and the control-plane note), and the baseline-arm
clause.

**Nothing was dropped for space.** A fourth candidate merge was rejected outright: the two
facts together exceeded the 320-character cap, and the only way to fit them was to drop
content, which is not compaction.

### 3.3 The compaction is machine-checked, fail-closed

`CARRIED_FORWARD` lists **47** clauses that must still appear on a named surface after the
rewrite, checked as substrings so wording stays free and coverage does not. It covers every
topic S3Y is required to preserve — D35, D38, D39, D44/body-blindness, F1/F2, structured
rows, the response schema, held-out thresholds and gates, eval-v5's retirement, eval-v6's
sole eligibility, the candidate-005 prohibition, the LR/rank/alpha/module/epoch/dropout
bars, the ATTENTION_ONLY / train-v3 bars, the no-unmeasured-eligibility rule, the separate
promotion authority, one-S3Y-evaluation-only and no-second-look.

A projection that drops one is **refused**, not reported.

---

## 4. Pre-evaluation preparation

### 4.1 eval-v6 was not on this host

`jarvis/training_gym_datasets/datasets/m62-defensive-eval/` held **v1–v4 only**. No v5, no
v6; the v6 manifest digest appeared in no file under the dataset store; `promotions.jsonl`
had a v5 row and no v6 row. Promoted dataset bytes are gitignored runtime state, and S3W.0
already recorded that v5's were absent from this host.

The evaluation runtime has **no in-memory path**: `build_task_pack_from_dataset` reads
shards from `<root>/datasets/<id>/<version>`, so an absent version is a plan blocker.

### 4.2 Why a dedicated dataset root

Rebuilding v6 into the standard root **fails, permanently**. `build()` materialises the
declared parent first, and re-promoting `v5` there raises `PlanAlreadyConsumed`: that root's
ledger already records v5's promotion plan as consumed, while its bytes are gone. A version
is immutable and its promotion plan is single-use, so the parent can never be
re-materialised in place.

This is anticipated by the architecture, not a workaround. Dataset identity is
**root-independent by design** — the Control Plane carries it as a standing limitation:
a holdout's `promotion_plan_hash` binds `output_root_id` and is *deliberately not part of
dataset identity*, while its manifest, pack and three set digests "are identical in every
root".

So v6 was built into a dedicated, gitignored root,
`jarvis/training_gym_datasets/s3y-eval-root/`, through the tracked generator and the full
authority chain. It was built **twice, into two independent roots**, and both reproduced
the frozen identity exactly:

```
manifest_hash             413e675711d51f5b98cb5a8ec7ff7fb0d8eb36b5e4c6dff790fb60f764f8fba6  ✓ frozen
pack_hash                 41579381422636d073d8ce3a0df230cafb97ffdd1489ab02126f2273565ade16  ✓ frozen
parent_manifest_hash      e852f4627d4fe631f58ee3d120d5d1a81c94480a1c0b84e590d2b08261043f4c  ✓ eval-v5
hidden_target_store_hash  49efb7215727218dd59ce630f407259282b7d39c82d22416a4cbf6652164dee0
task_count                36        splits 12 / 12 / 12        leakage clean, 0 findings
shard hashes              identical in both roots
```

The build is deterministic by construction: fixed `NOW`, fixed actor, a seed derived from
the dataset id and version, and material that is literal tracked source. The D36
host-identity stability check runs **before a byte is written** and refuses on any
instability; its refusal message carries only row id and field name.

Every step was run through a wrapper that prints an allow-listed set of body-free keys and
reports exceptions **by type only** — necessary because the generator's own refusal path
formats `(task_id, field, text)` triples, and `text` is a body.

### 4.3 The evaluation config

`m62-s3y-quality-heldout-live.json` did not exist and was authored. Evaluation configs are
**gitignored by design** (`configs/*.json`); only the template is tracked, and the S3I, S3L
and S3Q configs are untracked too. So this file is runtime state, not a tracked change —
but it does bind the plan through `evaluation_config_hash`, and it is therefore frozen from
the moment the authoritative plan is derived.

**It does not live under `jarvis/evaluation/`, and that is deliberate.** Writing it there
turned the preregistered S3W.0 gate
`test_no_candidate_004_evaluation_evidence_exists_anywhere` red: that test walks the whole
`jarvis/evaluation/` tree and refuses any file naming candidate 004. The gate is right. At
the barrier there must be no candidate-004 evaluation evidence anywhere, and a gate is not
relaxed because it is inconvenient to the milestone that tripped it.

So the config was moved OUT of that tree rather than the test being changed, and the gate
is green again — 50 passed. The plan hash is **unaffected**: `evaluation_config_hash` is
taken over the config's CONTENT, and re-deriving the plan from the new path returned the
identical `plan_hash` `239b5f9c…`.

That gate will legitimately go red the moment an authorised evaluation writes a run
directory naming candidate 004. Updating it *then* records history; updating it *now* would
have been post-hoc weakening of a preregistered gate, which the S3Y contract bars
explicitly.

The **generation block is byte-identical to S3Q's**: greedy, `max_new_tokens` 512, seed 11,
`timeout_s` 300 declared and unenforced, `reasoning_policy` disabled, cpu, fp32. Nothing was
tuned. Gate policy `e5003319` is unchanged.

### 4.4 The evaluation runtime was broken, and was not repaired by installing anything

Both M62 virtualenvs declare `version = 3.13.14` and were created against
`/usr/bin/python3.13`. The host's `python3` has since been upgraded to 3.14, and every venv
entry point — `bin/python`, `bin/python3`, `bin/python3.13` — now resolves to
`/usr/bin/python3.14`. A 3.14 interpreter does not read `lib/python3.13/site-packages`, so
`torch`, `transformers`, `peft` and `safetensors` were unimportable in every interpreter on
this host and `--check-dependencies` reported four blockers.

`/usr/bin/python3.13` still exists at exactly 3.13.14, and the reviewed packages are intact.
The evaluation is therefore invoked as the interpreter the environment was actually built
for, with `PYTHONPATH` pointing at that environment's own `site-packages`:

```
PYTHONPATH=<repo>/.venv-m62-eval-linux/lib/python3.13/site-packages  /usr/bin/python3.13
```

**Nothing was installed, no venv was modified, no global state was touched.** The compiled
extension ABI matches (cpython-313), and the resolved versions equal the ones candidate
004's training receipt records: `torch 2.13.0+cpu`, `transformers 5.14.1`, `peft 0.20.0`,
`safetensors 0.8.0`, `accelerate 1.14.0`.

This is a **deviation from the recorded invocation** and is flagged as one. It changes the
interpreter, not the dependency set.

---

## 5. Candidate qualification — body-free

```
--check-references
  baseline_reference_hash          7ba92ab72cc906d01ae7fa96279cf7cfef837961cbf09f054667d45f15d6c0a9
  candidate_adapter_reference_hash 3d2a32daa059580ba6b342a5090fa6ba30267fe7bb366255ea456371ec35779b
  baseline_revision_kind           immutable_commit
  adapter_artifacts_reverified     True
  pair_ok                          True
  blockers                         (none)          live_execution_blockers  (none)
```

No model weights were loaded or interpreted. Identity and digests only.

---

## 6. Holdout qualification — body-free

```
--check-task-pack
  dataset_manifest_hash  413e675711d51f5b98cb5a8ec7ff7fb0d8eb36b5e4c6dff790fb60f764f8fba6
  counts_by_split        adversarial 12 · hidden_evaluation 12 · security_regression 12
  splits_with_no_task    (none)          eligibility_blockers  (none)
  note                   counts only; held-out task material is never printed
```

eval-v5 stays retired and unspent; eval-v6 is the sole unused eligibility holdout. The
session-author disqualification invariant is active and was honoured: **this session did
not author eval-v6.** That separation is PROCEDURAL — the repository provides no
cryptographic proof of session identity, and none is claimed here.

---

## 7. Runtime feasibility — no model-facing access

```
--check-dependencies   ready True, blockers (none), report 78312447805c5b1a9a31e1d46f82b819c268ccbdbb0a217624c4b93676b74e3e
--check-hardware       cuda UNKNOWN, precisions [fp32], report 6b717507a3cb73b7106b262205b3d81d0ec874be2ab5ef2cb756fd2fb55e4604
model cache            reviewed local cache, located out-of-band — one snapshot, c1899de2…, refs empty
                       (the path stays unrecorded, as S3W.0 requires)
disk                   165 G free
output root            <repo>/jarvis/evaluation — no m62-s3y-quality-heldout-live directory exists
replay guard           evaluation_runs.jsonl carries no S3Y plan and no S3Y holdout commit
```

No benchmark was run, no model was loaded, nothing was generated, and eval-v6 was not
smoke-tested.

---

## 8. Authority state at the barrier

```
MODEL_WEIGHT_LOADS       0
MODEL_GENERATIONS        0
EVAL_ATTEMPTS            0
HOLDOUT_SPEND_EVENTS     0
EVAL_AUTHORITY_CREATED   NO
EVAL_AUTHORITY_CONSUMED  NO
EVAL_V6                  FROZEN_UNUSED   spent_by null
CANDIDATE004             TRAINED_UNEVALUATED
```

The confirmation string was never materialised. `--live-preflight` was the only plan surface
used, and it asserts its own silence: the preflight payload is scanned for a
`EVAL:<64 hex>` literal and the run is refused if one is present. `--dry-run`,
`--print-plan`, the bare/default mode, `--execute` and `--confirm` were **not** invoked;
the first three all materialise the token, the bare mode included.

Token silence here is **ceremony hygiene, not cryptography** — the repository says so
itself. The confirmation string is a pure function of a plan hash this document prints. The
barrier is procedural: the operator, not the assistant, decides.

---

## 9. Limitations of this pre-authorisation

* Capacity is proved for four terminal states. `EVALUATED_NEEDS_MORE_EVIDENCE` and
  `EVALUATED_QUARANTINED` are also reachable and were **not** separately projected; both
  are structurally the size of `NOT_ELIGIBLE`, which clears the floor by 41 bytes.
* The projected `spent_by`, plan digest and report digest are stand-ins **of the right
  shape**. The emitter refuses to write a snapshot carrying one.
* Gate thresholds remain uncalibrated and the holdout is 36 synthetic tasks by one author.
  Qualification is readiness, never evidence.
* The eval-v5/eval-v4 exposure in §2 is recorded, not repaired. It cannot be repaired.
* The interpreter deviation in §4.4 is a real difference from the recorded invocation.
* The dedicated dataset root in §4.2 means `promotion_plan_hash` differs from the frozen
  root's by design. Manifest, pack and shard digests do not, and were verified equal across
  two independent roots.
* **An open firewall gap, found and deliberately NOT patched here.**
  `FORBIDDEN_BODY_SYMBOLS` in `verify_m62_control_plane.py` names the eval-v4 and eval-v5
  body symbols, so no scanned surface may cite them — but it does **not** name eval-v6's.
  The tuple is documented as append-only and two sealed test files index it positionally,
  so the fix is a one-line append. It is not made here: `verify_m62_control_plane.py` is a
  control-plane path, the change would perturb sealed tests, and doing it inside the
  single-use ceremony it is meant to protect is the wrong order. It belongs to its own
  operator-governed milestone. The live exam is currently guarded by the task-id scan and
  the 320-character cap, not by a symbol scan.
* The Gen14 projector is proved against a projection, never against a written snapshot.
  It has never emitted one, and `--emit` refuses stand-in digests, a stand-in commit, and a
  failed capacity gate.

---

## 10. The authoritative plan

Derived on this execution host from the final clean HEAD, with `--live-preflight` only.
See the machine block in §11 for the measured `PLAN_HASH`, its reproduction check, and the
final repository state.

**Nothing beyond this point has happened.** The next step is an explicit human ruling.

---

## 11. The measurement — executed under one human authority

An operator supplied `EVAL:<plan-hash>` naming exactly the plan §10 published. The token
was passed **verbatim** to the canonical evaluator, which validated it against the plan it
recomputed; no confirmation string was ever constructed, concatenated or displayed here,
and the receipt records `token_literal_recorded false`.

Before the spend, state was re-verified read-only and a **third** `--live-preflight` was
run. It returned the preauthorised plan hash unchanged, `is_executable true`, zero
blockers. Only then was `--execute --confirm` invoked, **exactly once**.

```
states_visited   preflight_verified -> starting -> running_baseline -> running_candidate
                 -> scoring -> comparing -> artifact_validation -> completed
measured_pairs   36 of 36        missing_pairs 0        interrupted False
plan_consumed    True            rerun_permitted False  quarantined False
holdout_model_facing_committed True     holdout_scientifically_spent True
durability_problems 0           recovery_required False  terminal_ledger_recorded True
```

`eval-v6` is **spent**. It was single-use, and no rerun, ablation, re-score or second look
is possible or permitted.

### 11.1 The runtime had to be recovered first

The reviewed evaluation venv had been orphaned by a system Python upgrade from 3.13 to
3.14: its 95 packages are intact on disk, but `bin/python`, `bin/python3` and even
`bin/python3.13` all resolve to the new interpreter, which finds no `site-packages`. The
runtime was reached through `/usr/bin/python3.13` with the venv's `site-packages` on
`PYTHONPATH` rather than by modifying the venv, because S3W.0 holds that tree immutable.

That it is the SAME runtime is evidence, not assertion: `dependency_report_hash`
`78312447…` and `hardware_report_hash` `6b717507…` both re-derive to the values recorded
before this session. The receipt pins the backend as
`transformers=5.14.1 peft=0.20.0 torch=2.13.0+cpu`.

---

## 12. The result — ELIGIBLE FOR HUMAN REVIEW, which is a request

**Every deterministic gate passed.** `security`, `coverage`, `family`, `quality`,
`statistical` and `operational` were all evaluated; `blocking_count` is 0 and
`security_blocking_count` is 0. Candidate 004 is
`EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW`.

| | |
|---|---:|
| measured pairs | 36 / 36 |
| improved | 9 |
| regressed | 14 |
| unchanged | 10 |
| security_improvement | 3 |
| security_regression | **0** |
| wins / ties / losses | 9 / 10 / 14 |

```
paired mean delta   +0.0751        median delta   0.0
95% interval        [-0.0040, +0.1793]   (paired bootstrap percentile, 2000 iterations)
excludes_regression_margin   False
```

**Read the table before the verdict.** The gates passed, and *more tasks regressed than
improved* — 14 against 9. The observed mean delta is positive but its 95% interval
**does not exclude a regression**, which the run records as the standing gate warning
`regression_not_excluded`. The production decision text is explicit that this outcome
"is not approval, not promotion and not activation".

So the honest reading is: candidate 004 cleared the preregistered bar and produced no
security regression, and it did **not** demonstrate a quality improvement. Whether the
learning-rate change from 1e-4 to 5e-5 helped is **not** established by this measurement.

### 12.1 What this result is not

* Not a promotion, activation, registry mutation or release. `promotes_model false`,
  `mutates_model_registry false`, `activates_model false`. **No promotion authority exists
  or was requested.**
* Not calibrated. `thresholds_are_calibrated` is false; every threshold is an initial
  policy value.
* Not a head-to-head ranking against candidates 001-003: each was measured on a DIFFERENT
  holdout sharing zero task instances. The only valid comparison is against the baseline
  measured simultaneously on eval-v6.
* Not evidence about the six `tool_call_schema` tasks (D28 VACUOUS), about timeouts (D33
  VACUOUS), or unbounded by D29's refusal-detector limits.
* Not a dose-response curve. One learning-rate point on a CONFIGURATION axis.

---

## 13. Evidence identities

```
evaluation_id              m62-s3y-quality-heldout-live   generation 1
plan_hash                  e2b591fe7e6cf749696f813d92cc192d967a0f2959de22d4337766708e6e06de
evaluation_config_hash     767cca2397cdc767bdce50cde05c758415593ed7ad02ebe512259f4947dc5118
report_hash                d708d72162d9cf018a7e5065e1ccf860a20f94ee2f7a493b0d4f48ee532aa0af
evaluation_manifest_hash   22994a5eb15317463180b547cc156421d496d7e29c2f78586324e9c9100ef595
artifact_tree_hash         02fdf79e269278342e56c8b4ef3c080533572bfd0ee69d7eef8035c77f576a32
gate_report_hash           bda87cdb5d94c870deb95091780fec7a115d74bccca0f20bc2bdfc5ba5c0e610
decision_hash              5456dfd4c2db0314b38eacb6706d9eabd7601f48f1fa29199e86abad3bb42825

receipt                    state/m62/receipts/qwen3-06b-lora-quality-live-004.eval.json
receipt_version            m62.eval_receipt.3          receipt verify PASS, problems 0
receipt_hash               21c03e921ab37c996811ae99569d8c044e1a02325b252c45d56dec6ffa3fb109
receipt sha256             94725228d6808177a816c36e5f40facadac7c48eb5ad804fb096b6a0bda84b44

witness                    state/m62/witnesses/0002-s3y-live-measurement-witness.json
witness_hash               bf5378d2328680cfb98b08f2edd32345faaa05ad39caccc4c8730844b6eeae7a
witness sha256             0360e800554330a4684c559e0af8115dda31940df4eb8986b77a9bfd9a23c203

evaluation_source_commit   56b0498739a2b39045cef83b23ad692b33ede75e   (the commit that MEASURED)
seal_implementation_source f7a7d2cd14b8c65ebe08dbf5a7eb2a5048ac906c   (the commit that SEALED)
```

The two source commits differ, and the receipt records both separately. That split is
exactly what D42 exists for: a post-live seal must never name the repair commit as the
thing that measured. The witness was committed FIRST, while HEAD still stood at the
evaluation source, which is why the receipt could bridge at all.

The evaluation generation directory is gitignored runtime state. It holds the held-out
prompts and both models' responses and is reviewed locally only; nothing in this document
quotes it.

---

## 14. Machine block
