# V69 M62 S3V — Candidate 004, live training

**One training run, under one plan-bound human authority, created once and consumed once.
No evaluation. No promotion. `eval-v5` untouched.**

```
S3V_CANDIDATE004_LIVE_TRAIN     PASS
TRAINING_SOURCE_COMMIT          80565d32795fb276df202f6bef46ed38bb2bb7c5
START_STATE_GENERATION          9          END_STATE_GENERATION      10
CANDIDATE                       qwen3-06b-lora-quality-live-004
DESIGNED_UNTRAINED  ->  TRAINED_UNEVALUATED
PRIMARY_AXIS                    learning_rate    1e-4 -> 5e-5
EVAL_ATTEMPTS                   0          EVAL_V5   FROZEN_UNUSED, spent_by null
```

---

## 1 — What this milestone claims, and what it does not

It claims exactly two things: **the training run executed to its preregistered budget, and
the adapter it produced is structurally valid.** That is the whole content of
`TRAINED_UNEVALUATED`.

It claims **nothing about whether candidate 004 is better, safer, eligible or
production-ready.** Its training loss and its two validation losses are diagnostic
numbers, they are recorded below because hiding them would be worse, and **they are not
eligibility evidence**. Candidate 004's eligibility is **entirely UNKNOWN** and stays
unknown until `eval-v5` is spent exactly once, in a different session, under a fresh
single-use `EVAL` authority that does not yet exist.

The preregistered question S3U asked — does a smaller update preserve candidate 003's
measured improvements while redistributing less of the base model's stopping failure
mode — is **not answered here and cannot be**. S3V ran the experiment; it did not read the
result.

---

## 2 — The human authority

The operator supplied the exact plan-bound phrase
`AUTHORIZE_S3V_CANDIDATE004_TRAIN_ONCE:<plan-hash>` carrying the full 64-hex hash of the
plan derived below. Nothing weaker was accepted, and a phrase naming a different plan
would have authorised nothing.

```
TRAIN_AUTHORITY_FORM        TRAIN:<plan-hash>
BOUND_PLAN_HASH             0130fb5aac21ef79a20719c9d3e3d5e15b9f322d758cf6891792d6d2b815d498
CREATIONS                   1        CONSUMPTIONS            1
RETRY_AUTHORIZED            NO       TOKEN_LITERAL_RECORDED  NO
```

**Two separate human decisions, kept separate.** The S3U operator ruling authorised a
**design** and carries `scope: DESIGN_ONLY`; it did not and could not authorise training.
This milestone rests on a second, independent, plan-bound decision. The capability itself
is a pure function of the plan hash — token silence is **ceremony hygiene, not
cryptography** — so no tracked file, ledger line or evidence surface reproduces it.

The authority is now **SPENT**. There is no retry, and a failure at any later step of this
milestone would not have returned it.

---

## 3 — The plan, derived twice before it was authorised

Derived independently through two production paths, which produced **byte-identical plan
bodies**, not merely equal digests:

| path | origin | plan hash |
|---|---|---|
| A — generator | `build_config()` in code | `0130fb5a…d815d498` |
| B — document round-trip | emit → `load_training_config()` → `plan_training()` | **identical** |

```
PLAN_BLOCKERS     0
PLAN_WARNINGS     1  — "a CPU smoke run is slow; this validates the pipeline, and it is
                        not a route to a production adapter"   (reported, not suppressed)
selected device / precision   cpu / fp32        effective batch   1 x 8 = 8
performs_training / creates_adapter / contacts_network  (at plan time)  false / false / false
```

**Expected optimizer steps, arithmetic rather than estimate:** 154 TRAIN rows at effective
batch 8 gives `ceil(154/8) = 20` steps per epoch; 2 epochs lands exactly on 40, the
config's `max_steps`.

`config_hash` and `plan_hash` are **root-bound**. Candidate 003's `config_hash` re-derived
byte-exact on this host (`6f9f470f…`), which is how the environment was shown to be the one
S3P trained in. Its *plan* hash legitimately differs from the sealed `414ce9e3…` for one
benign reason: candidate 003 now **has** a run directory, so its plan is correctly
`blocked`. Every other bound component matched S3P, and candidate 004's
`feasibility_report_hash` `31dc790e…` is exactly the value S3P recorded for candidate 003
immediately before its own authorised training.

---

## 4 — The single axis, re-derived from production authority

```
SINGLE_AXIS_DIFF_COUNT      1
SINGLE_AXIS                 learning_rate
```

| surface | compared | differing |
|---|---|---|
| option dials | 9 | **1** — `learning_rate` |
| canonical config body | 43 keys | **1** axis + `run_id`, `experiment_name`, `notes` |
| training plan | full body | `learning_rate`, plus run identity and config hash |

Rank 16, alpha 32, `alpha/r` 2.0, dropout 0.05, weight decay 0.0, warmup 0.1, 2 epochs,
`max_steps` 40, gradient accumulation 8, batch 1, sequence 512, seed 42,
`ATTENTION_AND_MLP` over the same seven projections, `train-v2` and reasoning `DISABLED`
are **inherited by dictionary expansion from candidate 003's option, not re-typed**, so
there is no second place for them to drift to. `verify_single_axis` refuses to build any
configuration where that stops being true, and refuses a learning rate other than the
ruled `5e-5`.

**alpha is deliberately NOT slaved.** A learning-rate change needs no compensating
adjustment; `alpha/r` stays 2.0 because neither term moved. A "compensating" second dial
would be a second axis wearing a justification.

**D37 is FIXED and was not reopened.** Candidate 004's render policy is the *same object*
as candidate 003's — assigned from the reference, not spelled again — so train/eval parity
is one fact rather than two that could separate.

---

## 5 — Material, base model and runtime

```
TRAINING_CORPUS      m62-defensive-quality-train v2      182 promoted records
manifest             24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f
export manifest      82780fa0edc4c99198d0074a8a01b08507fa3eed54b4af50c3e045d5e07ae921
train shard          a02797f85d11498103918df9114ed4496e232a9a2c88b738f36f8326a72e1c7e
validation shard     ae6ffe204df4d2b60b2215aa38a641331cf56d999cc022c24f538fba891bb764
rows                 154 TRAIN converted · 12 VALIDATION · 0 truncated
```

No `train-v3` exists and none was created; no row was added, deleted or rebalanced.

```
BASE_MODEL           Qwen/Qwen3-0.6B @ c1899de289a04d12100db370d81485cdf75e47ca
base identity        9701f4f3…      tokenizer identity   45894db9…
model cache          present, evidence f399355e… (== the digest S3G.1 and S3P recorded)
revisions cached     exactly c1899de289a04d12100db370d81485cdf75e47ca
MODEL_DOWNLOADS      0              downloaded_anything  false
```

Runtime, measured on this host and identical to the one candidate 003's receipt records:

```
python 3.13.14 · torch 2.13.0+cpu · transformers 5.14.1 · peft 0.20.0
accelerate 1.14.0 · safetensors 0.8.0 · tokenizers 0.22.2
device cpu · precision fp32 · cuda_available false
local_files_only true · trust_remote_code false · model_download_policy deny
HF_HUB_OFFLINE=1 · TRANSFORMERS_OFFLINE=1 · HF_HUB_DISABLE_TELEMETRY=1
```

The absolute cache path is deliberately not recorded here — only its evidence digest.

---

## 6 — The `eval-v5` firewall

```
EVAL_V5_SEMANTIC_ACCESS   NO
EVAL_V5_STATUS            FROZEN_UNUSED      spent_by   null
EVAL_ATTEMPTS             0
```

`eval-v5` was bound by **manifest, pack and status only** —
`e852f462…` / `287a9fb6…` / 36 tasks. No prompt, target, hidden target or task body was
read; no task pack, materialised pack or evaluation output directory was opened; the file
holding the `v5` material builder was never opened, and this document deliberately does not
name that body-source symbol, so it is safe to scan as well as safe to read. No `EVAL` plan
was derived and no `EVAL` capability exists.

**Training does not spend the holdout.** Candidate 004 is trained and `v5` is still fresh;
those are two independent facts and S3V kept them that way.

---

## 7 — The run

```
TERMINAL_STATUS            SUCCESS        backend_status     succeeded
completed  true            interrupted    false              error_category  none
OPTIMIZER_STEPS            40 of 40 planned (40 requested)
EPOCHS                     2.0 of 2 configured
converted_records          154            truncated_records  0
duration                   3065.844 s  (~51 min, CPU)
seed                       42
```

Losses — **diagnostic only, and not eligibility evidence**:

```
TRAIN_LOSS                 3.591112
VALIDATION_LOSSES          3.406055212020874  (epoch 1, step 20)
                           3.310306787490844  (epoch 2, step 40)
FINAL_VALIDATION           3.310306787490844  · closing evaluate() present (D31)
```

Train loss by logging step fell 4.006995 → 3.529102 → 3.299920 → 3.537686 across the eight
logged steps. **No conclusion is drawn from any of these numbers**, and none of them may be
used to tune candidate 004 or to select it.

Validation stayed exactly what it has always been:

```
validation rows                          12       evaluations   2       strategy  epoch
contributes_gradients                    false
early_stopping                           false    load_best_model_at_end   false
is_held_out_eligibility_evidence         false
generation_performed                     false    model response tokens generated   0
```

Representation, verified in-run rather than asserted:

```
reasoning_policy            disabled
assistant_only_loss         true      strategy  manual_label_masking(-100)  verified
chat_render_policy_hash     8619f96c… (identical to candidate 003's)
full_sequence_policy_hash   c5e83324…
deterministic_reproduction_claimed        false
```

---

## 8 — The adapter

```
ADAPTER_SHA256            a105e01ca99d9b47d45c408a614b78aa9ec22df83ad32b321df57b1a1c3ecc67
ADAPTER_MANIFEST_HASH     162e93e36f284b651051a93e22cfc6cb15adef3f457038297ca72774e276b510
ARTIFACT_SET_HASH         326678618101eb4eec0a12b89a5e02f89340148111d5f4adf97d6a04f449b864
ADAPTER_BYTES             40,422,168        (run directory total 40,434,130)
```

**Newly derived, and not candidate 003's.** Candidate 003's adapter is `6ccd8fdc…`; a
fourth candidate inheriting a third's weights digest is exactly the substitution the
control plane's candidate/adapter pair exists to catch.

```
LORA_TENSORS      392        LORA_A   196     LORA_B   196     NON_LORA   0
TRAINABLE_PARAMS  10,092,544            TOTAL_PARAMS   606,142,464
rank 16 · alpha 32 · dropout 0.05 · bias none · CAUSAL_LM · use_rslora false
target modules    q_proj k_proj v_proj o_proj gate_proj up_proj down_proj   (7 x 56 tensors)
dtypes            F32 only              non-finite tensors   0
```

Artefact hygiene, checked rather than assumed:

```
safetensors only · 0 .bin · 0 .pt · 0 .ckpt · 0 pickle
checkpoint directories 0 (D16)   nested directories 0   symlinks 0
no base-model weight dump · no absolute host path in any run file
COMPLETED_RUN_VERIFIER   PASS        ADAPTER_VERIFIER   valid
```

---

## 9 — The portable training receipt

```
state/m62/receipts/qwen3-06b-lora-quality-live-004.train.json
schema        m62.train_receipt.1        (the existing contract, NOT a new version)
bytes         5,307
sha256        0cf3c8069cec487a49b8378cc9cbb44fa7849b47c96cbbf6fe5915c2a21edd6e
design        S3U @ 43cb590b9a8da7b912dd146a2e5ac410680729c4
training      S3V @ 80565d32795fb276df202f6bef46ed38bb2bb7c5
```

The receipt is the part that travels: the adapter, its manifest and the run ledger are
gitignored runtime artefacts, so a fresh clone has none of them.

### 9.1 A false-provenance defect, found and fixed before sealing

The receipt builder carried `DESIGN_MILESTONE = "S3O"` and `TRAINING_MILESTONE = "S3P"` as
**module-level constants**. They were correct for the only candidate that had ever used
them and silently wrong for every later one: the first candidate 004 receipt this milestone
generated attributed its design to **S3O** and its training to **S3P**. That is false
provenance, and it would have been sealed into portable evidence.

Fixed at the source rather than patched in the output:

* the **design** milestone is now re-derived from the production generator's own
  `CANDIDATES` entry, so the receipt records a derivation rather than a second copy that
  could disagree with the design authority;
* the **training** milestone is a per-candidate map, because it is a property of the
  execution and not of the design;
* a candidate absent from that map is a **refusal**, never a default — guessing a milestone
  is the failure this replaced.

Candidate 003's receipt **rebuilds byte-identical to its tracked bytes** after the change,
which is the regression that matters: the fix corrects future attribution without
re-identifying sealed history.

### 9.2 Non-vacuity — 30 corrupted bindings, 30 refusals

Every mutation was applied to a **throwaway copy**; the real receipt was never touched.

```
UNMUTATED baseline                      0 problems
NON_VACUITY                            30 / 30 mutations REFUSED
```

Refused: candidate id · design commit · training source commit · plan hash · authority
creations · authority consumptions · `retry_authorized` · base revision · corpus manifest ·
**export manifest** · **train shard** · **validation shard** · dataset version · reasoning
policy · render policy hash · adapter SHA · adapter manifest · **artifact-set hash** ·
tensor counts · `lora_a` count · non-LoRA count · **adapter parameter count** · **LoRA
rank** · terminal status · optimizer steps · **epochs completed** · `interrupted` ·
`generation_performed` · held-out evaluation runs · **ledger start count**.

### 9.3 Eight of those refusals did not exist before this milestone

The bolded entries above were **accepted** by the verifier as it stood. `check_training_receipt`
bound the corpus *manifest* but not the export or either shard; it required an artifact-set
hash to be *present* but never checked its identity; it bound `epochs_configured` but not
`epochs_completed`; and it read the ledger's plan hashes without ever counting its events.

Worse, the gap was **invisible**: the check returns early when no candidate is
`TRAINED_UNEVALUATED`, and none had been since generation 3. It had been reporting `PASS`
over an empty set. S3V is the first milestone since S3P to put a live candidate through it,
which is how the gap surfaced at all.

Closed here, with the file's own division of labour respected — re-derive where the
repository can, pin where it cannot:

* `FROZEN_TRAIN_EXPORTS` seals the export manifest and both shard digests. Pinned, not
  re-derived, because the dataset store is gitignored and a fresh clone cannot rebuild
  them — pinning is what makes the binding portable. A legitimate corpus change is a new
  version with its own row, never an edit to this one.
* `FROZEN_ADAPTER_ARTIFACT_SETS` seals the artefact-set digest per candidate, for the same
  reason: it cannot be re-derived without the run tree.
* LoRA rank, alpha and dropout are **re-derived from the generator's option**, since the
  repository can build them.
* `adapter_parameter_count` must equal `trainable_parameters`; `epochs_completed` must equal
  the configured epochs; the ledger must record exactly one start, exactly one completion,
  and exactly one distinct plan.

---

## 10 — Two-phase state advance

Two-phase, so the state can never describe itself.

```
PHASE A   the receipt, the builder fix, this document, the verifier extension  -> subject commit
PHASE B   the generation-10 snapshot, current.json, PROGRESS, the history index.
```

Between the phases the verifier **deliberately fails** `CANDIDATE_STATE`: the snapshot still
said `DESIGNED_UNTRAINED` while a run directory, a ledger entry and a receipt existed for
the identity, and the sealed milestone authority already said `TRAINED_UNEVALUATED`. That is
the stale-state detector working exactly as designed, and generation 10 clears it.

```
STATE_GENERATION   10
PARENT             4b6f1c9b1d5e512ecd22a66849245709204ed69c1c5ad25dd26cca9766022c98
candidate004       DESIGNED_UNTRAINED -> TRAINED_UNEVALUATED
evaluation_corpus  null        eval-v5   FROZEN_UNUSED, spent_by null
```

Generations 1–9 were not touched and the archive was not touched. `PROGRESS.md` gained
current state only — no milestone report — and the deep narrative it shed moved into the
documents that own it.

### 10.1 Capacity was proved BEFORE the authority was created

Generation 9 closed at **32,749 of 32,768** snapshot bytes — 19 bytes of headroom — and
`PROGRESS.md` sat at exactly 610 lines against a test that requires 150 lines of headroom
under a 760-line budget. Training was therefore not begun until a truthful generation-10
state had been shown to fit, using **in-memory projections only**, with no tracked file
mutated.

The strategy raised **no budget** and dropped **no fact**. Duplicate normative clauses that
had been restated three and four times over — "no ablation against a spent holdout", "each
candidate compares only to its own simultaneously-measured baseline", "re-derive, never
paste" — were stated once; deep narrative moved to the milestone documents that own it. A
token-level check confirmed **zero** distinctive facts lost: every defect id, digest,
figure, ratio and identifier present in generation 9's limitations and `ruled_out` is still
present in generation 10's.

One constraint shaped the whole approach: snapshot strings are capped at **320 characters**,
so entries could not simply be concatenated. The saving had to come from tighter prose.

```
PROJECTED_GEN10_HEADROOM_BYTES        >= 1024   (target)
ACTUAL_GEN10_HEADROOM_BYTES           see the closing block
PROGRESS line headroom                >= 150    invariant preserved
```

---

## 11 — Limitations

* **Candidate 004's eligibility is UNKNOWN.** A successful S3V means training completed and
  the adapter is structurally valid. Nothing more. Train and validation loss are diagnostic,
  are not eligibility evidence, and may not be used to tune or select.
* **One host, one CPU, one seed, one run.** No repeat, no ablation, no second host, no GPU,
  no dtype control arm, and `deterministic_reproduction_claimed` is `false`. A second run of
  the same plan is not asserted to produce the same weights.
* **The single-axis proof covers the CONFIGURATION, not trained weights.** It proves exactly
  one input differed. It cannot prove only one thing about the resulting model differs.
* **No dose-response is measured.** The learning rate had never been varied in this lineage.
  Candidate 004 adds one point, not a curve — and S3S.1 rated the risk to candidate 003's
  three security gains from a weaker update as **HIGH**. Losing them would be an informative
  result, not a surprise.
* **`config_hash` and `plan_hash` are root-bound**, and a plan hash moves again once a run
  directory exists. Re-derive on the executing host; never paste.
* **The receipt was built after the run**, from artefacts that already existed, and needs the
  gitignored run tree still present on the host. It proves what those artefacts say, not that
  nobody touched them between the run and the seal.
* **`FROZEN_TRAIN_EXPORTS` and `FROZEN_ADAPTER_ARTIFACT_SETS` are pinned constants**, not
  re-derivations. They make the bindings portable; they do not make them independent. A
  reader who distrusts the pin must rebuild the export from the dataset store.
* **`STALE_STATE` detection remains PARTIAL.** Portable receipts close the gap for candidates
  003 and 004 specifically; gitignored runtime artefacts stay outside Git.

---

## 12 — EXACT NEXT

**A NEW session.** Do not evaluate in the session that trained.

```
1. eval-v5 qualification and a TOKEN-SILENT EVAL preflight for candidate 004.
2. STOP for a fresh single-use human EVAL authority at a new generation.
3. ONE paired run against eval-v5.   4. m62.eval_receipt.3.   5. a new generation.
```

Promotion is a further human decision that no authority in this repository grants.

**Ruled out**, and unchanged by this milestone: retraining, resuming, re-seeding or further
fine-tuning candidate 004 under its own id · tuning it from its training or validation loss ·
evaluating it without a fresh `EVAL` authority · reusing `eval-v4` · reading `v4` or `v5`
bodies in any session · mutating, re-scoping or threshold-tuning frozen `eval-v5` for any
reason including a candidate 004 result · any epoch, rank, alpha or dropout change · a second
axis · `train-v3` · ranking candidates 001–004 in one table · promotion, activation, registry
mutation, merge, tag, release or version bump.

The machine-readable list is `next_milestone.ruled_out` in the generation-10 snapshot, and
that list is the authority.
