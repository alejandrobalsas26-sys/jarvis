# V69 M63 S4C — Candidate 005, live training

**Status:** candidate 005 is `TRAINED_UNEVALUATED`.
**Control plane:** V3, generation 18.
**Branch:** `jarvis-v69-m63-world-state`. `master` untouched.

One authorised training run happened. It produced an artefact. It produced no evidence
about whether that artefact is any good, and this document does not claim otherwise.

---

## 1 — What this milestone claims, and what it does not

**It claims.** That exactly one training run executed for candidate 005; that it consumed
exactly one single-use human `TRAIN` authority bound to one plan hash; that it completed
without interruption; that the artefact it wrote verifies; and that a tracked,
root-independent receipt establishes all of that from a fresh clone.

**It does not claim.**

* That candidate 005 is better than candidate 004. **Nothing measured it.**
* That candidate 005 is eligible, safe, promotable or production-ready.
* That the training loss or validation loss say anything about quality. They are
  **diagnostic**; `VALIDATION` is steering material, appears in no gate, and
  `validation_is_held_out_eligibility_evidence` is `false` in the receipt.
* That `RECOMMENDED_REMEDY` has changed. It is still **TOOLING**, and candidate 005 is
  still **`TRAINING_EXPERIMENTALLY_ALLOWED_NOT_PROVEN_NECESSARY`**. A run happening does
  not retroactively make it indicated.

**A clean training run creates an artefact. It does not create a verdict.**

---

## 2 — The human authority

```
form                 TRAIN:<plan-hash>
bound_plan_hash      5a786af98351e14ad231c138549b9db0206a5bd96263b7b0a94b4e625e403423
creations            1
consumptions         1
retry_authorized     false
token_literal_recorded  false
```

The operator supplied the token out of band, in the exact form the ceremony requires. It
was passed to the canonical executor **verbatim**; it was never reconstructed, never
corrected, and it is not reproduced in any tracked file. The receipt proves an authority
was created once and spent once; reproducing the string would hand a reader the
capability instead of the evidence.

**The authority is now consumed and is not replayable.** No retry is authorised. A second
run for candidate 005 — another seed, another value of the axis, a candidate 005b, or a
retry because a number looks unappealing — is not authorised by anything, and a poor
validation loss is not an infrastructure failure.

---

## 3 — Revalidation before anything model-facing

Every check below ran **before** the executor was invoked, and all of them are token-
silent: no `TRAIN:` string was constructed by any preauth surface.

```
branch                jarvis-v69-m63-world-state
HEAD                  08a7e81f157184389ef14d54007478076314c434
origin divergence     0  0
worktree              CLEAN
master                3705114228edef2f665be349c5c4429b7b16777a   UNCHANGED
CONTROL_PLANE_VERIFY  PASS   PROBLEMS 0

runs for candidate 005    0
run-ledger mentions       0
receipts                  0
adapter artefacts         0

AUTHORIZED_PLAN_HASH  5a786af9…e5403423
CURRENT_PLAN_HASH     5a786af9…e5403423        third derivation, separate process
MATCH                 PASS
```

The plan was additionally re-derived through the **document round trip** — emit the
configuration, `load_training_config()`, `plan_training()` — and produced the same
`config_hash` and the same `plan_hash`, which is the same two-path check S3V used.

### 3.1 The staleness risk this host actually carries

`plan_hash` binds `available_ram_category`, and this host sits on the 8 GB boundary
(S4B §11.3). At authorisation time the machine reported 10 GB available —
`8_to_16gb` — which is the class the authorised hash was derived under. Had it been
`under_8gb`, the hash would have been `fa8f1dff…fe738384` and the correct response
would have been to **STOP** with `AUTHORIZED_TRAIN_PLAN_STALE` and re-derive, never to
train on a plan the token does not bind.

---

## 4 — The run

```
executor        scripts/train_experiment.py --execute --confirm TRAIN:<plan-hash>
backend         transformers_peft   m62.transformers_peft.1
started         2026-08-30T14:15:29Z
duration        1373.7 s   (~23 minutes)
status          succeeded          run_state completed        interrupted false
optimizer steps 40 / 40            epochs 2.0 / 2
records         154 converted      0 truncated
seed            42
train_loss      3.762973
eval_loss       3.5828990936279297  ->  3.483412742614746     (12 VALIDATION rows)
generation_performed        false
held_out_evaluation_runs    0
model_response_tokens_generated  0
downloaded_anything         false
```

**Expected steps, arithmetic rather than estimate:** 154 TRAIN rows at effective batch 8
gives `ceil(154/8) = 20` steps per epoch; 2 epochs lands exactly on 40, the config's
`max_steps`. It did.

The learning-rate schedule confirms the axis reached the optimiser: the first logged step
carries `learning_rate 2.5e-05` — the ruled value, not the reference's 5e-5 — decaying
linearly to 6.944e-07 at the end.

**Two losses fell. That is not a result.** Candidate 004's own validation loss also fell,
and it went on to be measured `ELIGIBLE` and then **held**. These numbers are recorded
because hiding them would be worse, and they decide nothing.

---

## 5 — The adapter

```
adapter_sha256        52d6da26dca20dce93de8845fa08e0b3e452d86472fd6e06d756a30e52688f2a
manifest_hash         7442246c3d85f1007fe6885714ffbdbe7c53c6bfd251e3c36ca29ab7b489f78f
artifact_set_hash     ce5f757cf0cc6d3e998aab8809b45ebb66edefdfb7ecaf0c2811840ca7ac79d9
bytes                 40,422,168   (total artefact set 40,434,124)
tensors               392 LoRA  (196 A + 196 B)   ·  0 non-LoRA
dtypes                F32 only
target modules        q_proj k_proj v_proj o_proj gate_proj up_proj down_proj
trainable / total     10,092,544 / 606,142,464
adapter verifier      valid          completed-run verifier   PASS
symlinks 0  ·  checkpoint directories 0  ·  nested directories 0  ·  no pickle
```

The digest is **not** candidate 004's `a105e01c…`. A fifth candidate inheriting a
fourth's weights digest is the substitution the sealed pair in the verifier exists to
catch, and it does not occur here.

The LoRA geometry is identical to candidate 004's — rank 16, alpha 32, dropout 0.05,
`alpha/r` 2.0, the same seven projections, 392 tensors — because those are exactly the
dials the single axis forbids moving. The adapter's *shape* is the control; its *weights*
are the experiment.

---

## 6 — Material and runtime, as executed

```
TRAINING_CORPUS   m62-defensive-quality-train v2      182 records, UNCHANGED
manifest          24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f
reference         b3e1be3ed7e41953f874493a398c2dc3bd2267321d32d45572a5b4ba95f54a5c
train shard       a02797f85d11498103918df9114ed4496e232a9a2c88b738f36f8326a72e1c7e
validation shard  ae6ffe204df4d2b60b2215aa38a641331cf56d999cc022c24f538fba891bb764

BASE_MODEL        Qwen/Qwen3-0.6B @ c1899de289a04d12100db370d81485cdf75e47ca
base identity     9701f4f3…        tokenizer identity   45894db9…
chat template     a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8
render policy     8619f96c5ba84dab9afe19f8a0fcf385cb452680dd50374ba0e0b9a568490db0
reasoning policy  disabled         assistant-only loss, manual masking (-100), verified
MODEL_DOWNLOADS   0                local_files_only true · trust_remote_code false

python 3.14.6 · torch 2.13.0+cpu · transformers 5.14.1 · peft 0.20.0
device cpu · precision fp32 · deterministic_reproduction_claimed false
```

`chat_render_policy_hash` is byte-identical to candidate 004's, which is what makes the
comparison a comparison: D37 train/eval parity is inherited, not re-decided.

The runtime is the one S4B rebuilt and qualified — `PY314_NATIVE`, the exact reviewed
pins, on a fresh project-local environment. The broken historical venv was measured and
**not mutated**, and it still is not.

---

## 7 — The portable training receipt

```
path              state/m62/receipts/qwen3-06b-lora-quality-live-005.train.json
sha256            fc3cd5f65f8ff6aac64885abb43e72260add4e09a3544f122d89913674a3dd5b
bytes             5,306
schema            m62.train_receipt.1
design_commit     9e50a74c2f24cab905b9aaaac18e2970255db24e     (S4B)
training_source   08a7e81f157184389ef14d54007478076314c434     (S4C)
```

Rebuilt from the same run directory it reproduces **byte for byte**, which is the
property the receipt's determinism rests on: it carries no timestamp and no
self-referential digest, so its identity is its bytes and the snapshot that points at it
is what records the digest.

`design_commit` and `training_source_commit` are deliberately different. They are two
different facts — the commit that specified the experiment and the commit the run
executed at — and D42 exists because collapsing them once made truthful post-run sealing
impossible.

The receipt carries **no** token literal, **no** private path, **no** task material and
no held-out identifier. It records `holdout.evaluation_corpus: null`,
`eval_authority_created: false`, `held_out_evaluation_runs: 0` and
`model_response_tokens_generated: 0` — a training run that generated nothing.

### 7.1 One fail-closed refusal, before the receipt existed

The receipt builder refused its first invocation:

> `'qwen3-06b-lora-quality-live-005' has no recorded training milestone; add it to
> TRAINING_MILESTONES rather than letting the receipt inherit another candidate's`

That is the guard working. `S4C` was added explicitly rather than allowing the receipt to
inherit `S3V`, candidate 004's training milestone. A design milestone and the run that
spends authority on it are separate events that fail separately, and a receipt that
misreported which one it evidenced would be a receipt about a different run.

---

## 8 — What did NOT happen

```
EVAL_AUTHORITY_CREATED     NO
EVALUATION_RUNS            0
HOLDOUT_SPENDS             0
eval-v7                    NOT_CREATED
eval-v6                    USED_IMMUTABLE, untouched, not re-read
eval-v5                    FROZEN_UNUSED, spent_by null, RETIRED, unread
eval-v4                    spent, unread
PROMOTIONS                 0
REGISTRY_MUTATIONS         0
PRODUCTION_ROLE_CHANGES    0    FAST · DEEP · CODER · VISION · EMBEDDING · VERIFIER
CANDIDATE_006              NOT_CREATED
SECOND_RUNS                0
MASTER                     unchanged, unmerged, untagged, unreleased
```

Candidate 005 is **INACTIVE**. Candidate 004 remains
`EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` with `HUMAN_DECISION = HOLD` and `PROMOTED = NO`;
this milestone did not reopen, re-measure or reinterpret it.

---

## 9 — EXACT NEXT

**A fresh, independent evaluation-design ceremony.** Candidate 005 exists, is trained, and
is scientifically **unmeasured**. Measuring it requires all three of:

1. a **fresh eligibility holdout** that does not exist — `eval-v4` and `eval-v6` are
   spent, `eval-v5` is retired unspent, and no `eval-v7` has been created;
2. a **fresh independent session**, because **a holdout author is never its evaluator**
   and the session that designed and trained candidate 005 is permanently disqualified
   from authoring or running its exam;
3. a **separate explicit human roadmap ruling**, which no result implies and which
   nothing in this milestone pre-authorises.

Promotion requires a fourth thing that also does not exist: an explicit human promotion
authority. `PLAN CONSUMED != HOLDOUT SPENT != EVALUATION COMPLETED != PROMOTION
AUTHORIZED` — four events, recorded separately because they fail separately. This
milestone completed the first and only the first.
