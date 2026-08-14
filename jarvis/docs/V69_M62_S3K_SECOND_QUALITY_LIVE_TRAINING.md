# V69 M62 S3K — Second quality-candidate live training run

> **Status:** execution milestone. **One single-use `TRAIN` authority was created and
> consumed exactly once, and `qwen3-06b-lora-quality-live-002` trained to completion.**
> Nothing was evaluated against held-out material, no response token was generated, and
> nothing was promoted.
>
> **`m62-defensive-eval v3` remains FROZEN and UNSEEN.** No `EVAL` authority exists.

| | |
|---|---|
| Date | 2026-08-14 |
| Branch | `jarvis-v69-m62-training-gym` |
| Starting HEAD | `4ec4b36bcd4e012f68e07ce9a3737c475f270319` |
| Host | Kali Linux mini-PC, CPU only |
| Preceding milestones | **S3J** (design) · **S3J.1** (training runtime) |
| Second candidate | `qwen3-06b-lora-quality-live-002` — `DESIGNED_UNTRAINED` → **`TRAINED_UNEVALUATED`** |

---

## 1 — Operator authorisation

The human operator authorised **exactly one** live training attempt for
`qwen3-06b-lora-quality-live-002` on `m62-defensive-quality-train v2`, using the
already-qualified `.venv-m62-train-linux`, accepting the CPU-only Kali host, and
permitting **exactly one** fresh single-use `TRAIN` authority to be created and consumed
once. **No retry is authorised.** External host cooling was noted as host cooling only and
changes no model or runtime identity.

**Not authorised, and every one of them is `NO` in §13:** a second `TRAIN` authority, any
retry or resume, changing training data / VALIDATION data / `eval-v3` / any
hyperparameter, live evaluation, `EVAL` authority, any model response generation, any
inference against `eval-v3`, early stopping, `load_best_model_at_end`, checkpoint-based
retry, modifying candidate 001, promotion, activation, registry mutation, merge, tag,
release or version bump.

---

## 2 — Starting checkpoint, verified rather than assumed

```
git rev-parse HEAD                         4ec4b36bcd4e012f68e07ce9a3737c475f270319
git rev-parse origin/…-training-gym        4ec4b36bcd4e012f68e07ce9a3737c475f270319
git rev-list --left-right --count …        0   0
git rev-parse origin/master                3705114228edef2f665be349c5c4429b7b16777a
git status                                 clean, no untracked file of any kind
```

Nothing was reset, restored, cleaned, stashed, discarded or force-pushed. No tracked
source file was modified at any point in this milestone.

---

## 3 — The training runtime

`.venv-m62-train-linux` — the environment S3J.1 provisioned and qualified. Every version
was re-verified in this session rather than trusted from the brief.

| | Measured here | S3J.1 |
|---|---|---|
| Python | **3.13.14** | ✅ |
| `torch` | **2.13.0+cpu** | ✅ |
| `transformers` | **5.14.1** | ✅ |
| `peft` | **0.20.0** | ✅ |
| `datasets` | **5.0.1** | ✅ |
| `trl` | **1.9.2** | ✅ |
| `accelerate` | **1.14.0** | ✅ |
| `safetensors` / `tokenizers` | 0.8.0 / 0.22.2 | ✅ |
| `sentencepiece` / `numpy` | 0.2.2 / 2.5.2 | ✅ |
| `jsonschema` / `huggingface_hub` | 4.26.0 / 1.27.0 | ✅ |
| CUDA available / device | **False / CPU** | ✅ |
| `torch` default dtype | `torch.float32` | ✅ |

**`.venv-m62-eval-linux` was not read, written, installed into or activated at any point.**
It remains the sealed runtime of the S3I measurement of record. Nothing was installed,
upgraded or removed anywhere during S3K.

```
build_dependency_report(TRAINING, SFT_LORA)   ready=True   blockers=[]
missing []   incompatible []   unknown []
torch · transformers · datasets · accelerate · peft · trl · safetensors · sentencepiece
   — all 8 `installed`, all at or above their declared floors
imports OK:  torch · transformers · peft · datasets · trl · accelerate ·
             training_gym.training.backends.transformers_peft · planner · datasets.export
trl.SFTTrainer present:  True
```

### 3.1 Offline sealing

Every command in this milestone ran under `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`,
`HF_HUB_DISABLE_TELEMETRY=1`, with `local_files_only=True` and `trust_remote_code=False`.
The run's own evidence records `local_files_only: true`, `trust_remote_code: false` and
`downloaded_anything: false`. **No model was downloaded, no snapshot fetched, no network
contacted.**

---

## 4 — Pre-token verification

Everything below was re-derived through the repository's own authorities **before any
authority existed and before anything was spent**. No value was quoted from a document and
accepted.

| Item | Expected | Measured this session |
|---|---|---|
| `train-v2` manifest | `24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f` | identical |
| `train-v2` parent | `9bbac2f057fd0592a30a7fdeb968655f8ea585df00966e1b920415377ab7286a` | identical |
| Rows / splits | 182 · TRAIN 154 · VALIDATION 12 · 8 + 8 internal | identical |
| TRAIN export hash / rows | `82780fa0…` / 154 | identical, verifies, 0 problems |
| TRAIN export file sha256 | `72065595893decf87b6276595634f01c8dbb2313cbfbbd482bbe660e63166410` | identical, re-hashed from bytes |
| VALIDATION export hash / rows | `ac065112…` / 12 | identical, verifies |
| VALIDATION export file sha256 | `7ee612efa0d0609d33fa06bee3057128b3ac0e90cdc54a23d4a5da6d15081c33` | identical, re-hashed from bytes |
| Dataset reference hash | `b3e1be3ed7e41953f874493a398c2dc3bd2267321d32d45572a5b4ba95f54a5c` | identical |
| Model cache | `present`, evidence `f399355ef441e8ec…` | identical to S3H's recorded evidence |
| Revisions in the cache | exactly `c1899de289a04d12100db370d81485cdf75e47ca` | exactly that one |
| Chat template digest | `a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8` | **exact match** |
| TRAIN lengths (full seq) | min 65 · median 112 · p95 159 · max 169 | identical |
| VALIDATION lengths | min 90 · median 109 · p95 150 · max 155 | identical |
| Truncations at 512 | 0 / 166 exported | **0 / 166** |
| Masking self-test | verified both splits | **0 problems** both splits |
| `output_root_id` | `1dd79ac5ccd871741e73fee7a8af596e4fd8233145a4567e5910a21d7c62c5ac` | identical |
| **Config hash** | `08be37d37dd403ea8b049ab7bb32498f5d767ef013876920783ad4669e608649` | **re-derived, identical** |
| Gate policy | `e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5` | identical; QG-2 still absolute |
| `eval-v3` (read only) | `7c948236…`, parent `82b60bfd…`, 36 records | identical |
| Candidate 001 adapter | `43213035c15cd38928d2d6a3bdbd9af96872a954801c6bfd0a9b82a8e22ac858` | identical — untouched |
| Candidate 002 run directory | must not exist | **absent** |

The absolute cache path is deliberately not recorded here — only its evidence digest,
which is what identifies it as the same reviewed cache without any document naming a
location.

### 4.1 The pre-token gate

```
S3K_PRETOKEN_GATE:            PASS
GIT_CLEAN:                    YES
TRAINING_VENV:                QUALIFIED
DEPENDENCY_REPORT:            READY   (0 blockers)
BASE_REVISION_MATCH:          YES
TRAIN_V2_MANIFEST_MATCH:      YES
TRAIN_EXPORT_MATCH:           YES
VALIDATION_EXPORT_MATCH:      YES
CHAT_TEMPLATE_MATCH:          YES
TRUNCATIONS:                  0 / 166
CANDIDATE_CONFIG_FROZEN:      YES
EXPECTED_STEPS:               40
LIVE_PLAN_HASH:               a07f924969387e2b42db5e86d98f1f438d464f94bc969e79f9a0f194f790ffcb
PLAN_BLOCKERS:                0
PLAN_WARNINGS:                1   (CPU-run caution — reported, not suppressed)
TRAIN_TOKEN_CREATED:          NO
TRAIN_TOKEN_CONSUMED:         NO
```

---

## 5 — Configuration — frozen, not one field moved

| | Value |
|---|---|
| Method | `SFT_LORA` |
| Base model / revision | `Qwen/Qwen3-0.6B` @ `c1899de289a04d12100db370d81485cdf75e47ca` |
| LoRA rank / alpha / dropout / bias | **16 / 32 / 0.05 / none** |
| Target modules | `q_proj k_proj v_proj o_proj gate_proj up_proj down_proj` |
| Task type | `CAUSAL_LM`, `use_rslora` false |
| Learning rate / weight decay / warmup | **1e-4** / 0.0 / 0.1 |
| Epochs / `max_steps` | **2** / **40** |
| Batch × grad-accum = effective | 1 × 8 = **8** |
| Seed / `max_sequence_length` | **42** / **512** |
| Device / precision | **CPU / FP32** |
| Optimizer / scheduler | `adamw_torch` / linear decay — transformers defaults, **not fields of `TrainingConfig`** (§12.5) |
| `checkpoint_strategy` / `gradient_checkpointing` | **`no`** / false |
| `validation_strategy` / `validation_split` | **`epoch`** / `validation` |
| Early stopping / `load_best_model_at_end` | **disabled** / **false** |
| Model download policy / remote code | `deny` / `false` |
| Config hash | `08be37d37dd403ea8b049ab7bb32498f5d767ef013876920783ad4669e608649` |

### 5.1 Expected optimizer steps, re-derived from the actual split

```
TRAIN rows                    154        read from the verified export, not asserted
effective batch               8
steps per epoch  ceil(154/8)  20
epochs                        2
EXPECTED OPTIMIZER STEPS      40         ← the re-derived plan's max_steps agrees
```

---

## 6 — The live plan — re-derived, never forced

Produced twice by the tracked generator `scripts/build_quality_training_config.py
--candidate 002 --plan` and twice by the production `scripts/train_experiment.py
--print-plan` — the authority `--execute` itself recomputes against. **All four agreed.**

```
LIVE_PLAN_HASH        a07f924969387e2b42db5e86d98f1f438d464f94bc969e79f9a0f194f790ffcb
PLAN_BLOCKERS         0
PLAN_WARNINGS         1
PLAN_IS_EXECUTABLE    true
```

| Binding | Value |
|---|---|
| Run id | `qwen3-06b-lora-quality-live-002` |
| Dataset manifest | `24ceb1e0…` · reference `b3e1be3e…` · verification `6880a7e0…` |
| TRAIN / VALIDATION split hash | `146f4785…` / `6e95f661…` |
| Internal held-out hashes | `b321d52c…` (hidden eval) · `705d5aed…` (security regression) |
| Base / tokenizer identity | `9701f4f3…` / `45894db9…` |
| Dependency report hash | `b6f206c7…` |
| Hardware / feasibility report hash | `6b717507…` / `31dc790e…` |
| Resource policy hash | `96c06400…` |
| Model cache / download required | `present` / **false** |
| Selected device / precision | **cpu / fp32** |
| Memory peak / disk estimate | 3.817 GB / 0.406 GB |
| Feasibility verdict | `feasible_with_warnings` |
| Expected output root id | `1dd79ac5…` |

### 6.1 The plan hash moved from S3J.1's, and that is correct

S3J.1 recorded `738b187f…` and S3J `f7209a64…`, both **reference only**. Neither was pasted
in and neither was forced. This session re-derived `a07f9249…`.

**Every substantive binding reproduced S3J.1 exactly** — config hash `08be37d3…`, dataset
reference `b3e1be3e…`, both split hashes, both internal held-out hashes, both identity
hashes, cache `present`, device/precision, the 3.817 GB and 0.406 GB estimates, the
verdict, and the blocker and warning lists.

The one binding that is host state is `hardware_report_hash`. Its identity deliberately
**excludes** `available_ram_gb` and `output_disk_free_gb` — the repository's own reasoning
is that a token which expires spontaneously between being derived and being typed teaches
an operator to re-plan and paste without reading, destroying the control the token exists
to be. What it does include is the *categories* (`available_ram_category`,
`output_disk_category`, `total_ram_category`), which are what feasibility actually decides
on. Those are the members that legitimately differ between two sessions days apart on a
working machine.

**The hash was stable where it had to be**: four derivations returned `a07f9249…`, and
`--execute` recomputed the same plan and accepted the token.

### 6.2 The warning, carried not suppressed

```
a CPU smoke run is slow; this validates the pipeline, and it is not a route to a
production adapter
```

One warning, the same one S3H ran under. It is a property of the hardware class, not a
defect, and it was reported rather than silenced.

---

## 7 — TRAIN authority

```
TOKEN_FORM:            TRAIN:<plan-hash>
BOUND_PLAN_HASH:       a07f924969387e2b42db5e86d98f1f438d464f94bc969e79f9a0f194f790ffcb
TOKENS_DERIVED:        1
TRAIN_TOKEN_CREATED:   YES
TRAIN_TOKEN_CONSUMED:  YES   (exactly once, 2026-08-14T23:34:11Z)
TRAIN_ATTEMPTS:        1
RETRY_AUTHORIZED:      NO
```

The token is **derived** from the plan by `TrainingPlan.confirmation_token()`, never issued
by one; computing a plan spends nothing. Exactly one was derived, by the main agent, passed
on the command line as the CLI requires, and handed to no subagent and no second process.
**The reusable string is deliberately not written into any tracked file** — what is
recorded is the plan hash it binds, which is already public in this document, and the
consumption state.

Consumption is proven by the run's own artefacts rather than asserted: `training_log.jsonl`
carries `plan_consumed` in state `starting`, the CLI outcome reports `plan_consumed: true`,
and the ledger gained exactly one `started` line for this run id against this plan hash.
**It can never return to `NO`, and no second attempt is authorised whatever the outcome had
been.**

---

## 8 — Execution

```
TRAINING_RESULT:  SUCCESS
```

| | |
|---|---|
| Started (UTC) | `2026-08-14T23:34:11Z` |
| Ended (UTC) | `2026-08-14T23:49:43Z` |
| Wall clock | **15 min 32 s** (932 s) |
| Backend-reported duration | 930.196 s (`train_runtime` 912.8 s) |
| Hard operator ceiling | 4 hours — **not approached** |
| Optimizer steps planned / attempted / completed | 40 / 40 / **40** |
| Realised epochs | **2.0** (exactly, as designed) |
| Converted training records | 154 |
| Truncated records | **0** |
| Backend status / warnings | `succeeded` / **none** |
| Run state | `completed`, `interrupted: false`, `error_category: none` |
| States visited | `created → config_validated → dataset_verified → dependencies_verified → hardware_verified → planned → awaiting_confirmation → preflight_verified → starting → running → artifact_validation → completed` |

**Realised epochs are exactly 2.0, not 2.897.** This is the designed difference from S3H
and it is arithmetic, not luck: 154 TRAIN rows at effective batch 8 give
`ceil(154/8) = 20` optimizer steps per epoch, so `max_steps = 40` lands precisely on the
epoch-2 boundary. S3H's 107 rows gave 13.375 steps per epoch, so its 40-step budget stopped
mid-epoch at 2.897.

**Wall time was measured, not assumed, and it came in below the estimate.** S3J.1
projected ~27–35 minutes by calibrating against S3H's 27m47s on the *Windows* host. The
measured cost per micro-batch is **≈2.96 s** here (308 micro-batches in 912.8 s) against
**≈5.4 s** there, so this Kali host is roughly 1.8× faster per micro-batch for the same
work. The estimate was not wrong about the workload; it was calibrated on a different
machine. **No abort question arose** — the run finished well inside every bound, and no
runtime limit was enforced.

---

## 9 — Losses

### 9.1 Training loss

Aggregate reported by the trainer: **`train_loss = 3.277057`**.

The logged curve, at the configured 5-step logging interval:

| step | epoch | training loss |
|---:|---:|---:|
| 5 | 0.259740 | 3.952389 |
| 10 | 0.519481 | 3.495433 |
| 15 | 0.779221 | 3.496253 |
| 20 | 1.000000 | 3.195724 |
| 25 | 1.259740 | 2.995971 |
| 30 | 1.519481 | 3.010795 |
| 35 | 1.779221 | 2.925896 |
| 40 | 2.000000 | 3.143991 |

The curve falls 3.952389 → 2.925896 and is **not monotone**: it rises slightly at step 15,
at step 30 and again at the final step 40. On a 12-row-per-step effective batch over a
154-row corpus that is ordinary batch-to-batch variation, and the run was not touched
because of it — the frozen plan governs, not the shape of an intermediate number.

### 9.2 Validation loss

```
VALIDATION_STRATEGY:              EPOCH_PLUS_FINAL
VALIDATION_ROWS:                  12
VALIDATION_ROWS_TRUNCATED:        0 / 12
PERIODIC_VALIDATION_EVALUATIONS:  2
GENERATION_DURING_VALIDATION:     NO   (teacher-forced loss only)
VALIDATION_CONTRIBUTES_GRADIENTS: NO
NON_FINITE_METRIC_DETECTED:       NO
```

| # | epoch | step | eval loss | eval runtime |
|---:|---:|---:|---:|---:|
| 1 | 1.0 | 20 | **3.090760** | 11.3106 s |
| 2 | 2.0 | 40 | **3.018860** | 11.3733 s |
| final `evaluate()` | 2.0 | — | **3.018860** | **11.5471 s** |

**Exactly two periodic evaluations fired, and the closing pass is the third.** Because
`max_steps = 40` lands on the epoch boundary, the epoch cadence fires at steps 20 and 40
and there is no fractional third boundary — the opposite arrangement to S3H, where a third
periodic evaluation fired at step 40. The closing `trainer.evaluate()` measured the same
weights as the step-40 evaluation and returned the same loss, with a **visibly different
runtime** (11.5471 s vs 11.3733 s), which is what shows they are two passes and not one
number printed twice.

**The closing pass must not be removed on the grounds that it duplicated the last periodic
one.** Its purpose is to guarantee an end-of-run number whatever the cadence does, and the
cadence's behaviour at the final boundary is a property of the installed transformers build
and of the arithmetic of a particular corpus size — not a repository guarantee. S3H and S3K
now demonstrate both arrangements.

### 9.3 What the two curves say, and what they do not

Validation loss fell at both measured points (3.090760 → 3.018860) and did not turn up.
That is the shape the two moved dials — LR 2e-4 → 1e-4 and 3 epochs → 2 over 44 % more
rows — were chosen to produce, and S3H's candidate-001 curve did the opposite
(3.205301 → 3.122892 → 3.125407, a 0.002515 uptick at the end).

**This is not evidence that candidate 002 is better than candidate 001, and it must not be
reported as such.** Three independent reasons:

1. **The two validation losses are not comparable quantities.** Candidate 001's was
   computed over the 9-row `v1` VALIDATION split; candidate 002's is computed over the
   12-row `v2` split. Different rows, different denominator, different corpus version.
2. **Twelve rows is a very small sample.** A movement over twelve rows is a weak signal —
   which is exactly why early stopping is refused. It is enough to see a gross train/eval
   divergence; it is not enough to rank two runs.
3. **VALIDATION is train-side steering material** (`TRAIN_SIDE_SPLITS`), it appears in no
   S3G §6 gate, and it authorises nothing.

Nor is the training loss comparable: 3.277057 here against 2.991393 for candidate 001 is a
different corpus, a different number of rows, a different learning rate and a different
number of passes. **A higher training loss is not a worse model, and a lower validation
loss is not a better one.** Everything about candidate 002's quality is **unknown, not
estimated**, until a fresh `eval-v3` run under a new `EVAL` authority says otherwise.

---

## 10 — Artefacts

```
ADAPTER_CREATED:            YES
ARTIFACT_VERIFICATION:      PASS   (verify_completed_run → 0 problems)
SAFETENSORS_ONLY:           YES
CHECKPOINT_DIRECTORIES:     0
FORBIDDEN_ARTIFACTS:        0
BASE_MODEL_DUMP_DETECTED:   NO
NESTED_DIRECTORIES:         0
SYMLINKS:                   0
```

### 10.1 The run directory

Flat — no nested directory, no symlink, no `checkpoint-*` entry.

| File | bytes | sha256 |
|---|---:|---|
| `adapter_model.safetensors` | 40,422,168 | **`319c252498ba51e01ed59f58fc20ae639e2d886bf67277d3aa6df2e9f9665409`** |
| `adapter_config.json` | 1,148 | `869a15785367031bea1587b3979e5a0a5db045155ae450666d05aeded3774c04` |
| `README.md` (the model card PEFT writes; D15) | 5,184 | `9bd3cee25f0a5d53eb86d621dfb4c893f037bf340cee6c3f580a517bec928061` |
| `backend_result.json` | 2,631 | `56d991158fd97585dad90861acf9f5d91c3b43a2b18780ea753b20eb7c7b05b7` |
| `run.json` | 1,620 | `415152a21e5c5f7bdbb74dd639bf252599f11e03b74b3db7e52a1fe9deba1e93` |
| `training_log.jsonl` | 826 | `615e954a75b0fb2e04de4aafb5d46cf755969a56c2c7a0614339c08db9123d59` |
| `adapter-manifest.json` | 3,584 | the manifest itself; digested as `manifest_hash` below |

| Digest | Value |
|---|---|
| Adapter manifest hash | `11897e16b081cc4df2517f1c0c0904b7b7580ab4daf8fea0157e49ee4e2f6ca8` |
| Artifact-set (tree) hash | `220350efe5e2dda594f17ca03f1cf6db15885403a908a3b6dcd899b4d498d6f4` |
| Total bytes | 40,433,577 |

**The adapter has a NEW identity.** `319c2524…` is not `43213035…`; candidate 001's bytes
are untouched and were re-hashed to their recorded value after this run. The two files are
the same *size* because they are the same architecture — r = 16 over the same seven
projections — which is a property of the shape, not of the contents.

### 10.2 Verification

`training_gym.training.artifacts.verify_completed_run` — the repository's own authority,
not a second opinion — returned **no problems**. It re-derives every claim the directory
makes: the manifest parses and its own digest matches, every file it names is present and
rehashes to the recorded digest and size, **no file is present that it does not name**, and
the tree digest re-derives from the bytes on disk.

The manifest binds the run to its authorities, and each was checked against the value this
session verified **before** training:

| Manifest field | Value | Binds to |
|---|---|---|
| `run_id` | `qwen3-06b-lora-quality-live-002` | the authorised candidate |
| `plan_hash` | `a07f9249…` | the live S3K plan |
| `training_config_hash` | `08be37d3…` | the frozen candidate-002 configuration |
| `dataset_manifest_hash` | `24ceb1e0…` | `m62-defensive-quality-train v2` |
| `dataset_id` / `dataset_version` | `m62-defensive-quality-train` / `v2` | the authorised corpus |
| `dataset_reference_hash` | `b3e1be3e…` | the verified reference |
| `export_manifest_hash` | `82780fa0…` | the 154-row TRAIN export |
| `train_shard_hash` / `validation_shard_hash` | `a02797f8…` / `ae6ffe20…` | the promoted shards |
| `hidden_evaluation_hash` / `security_regression_hash` | `b321d52c…` / `705d5aed…` | the internal held-out splits, bound by digest and never trained on |
| `base_model_id` / `base_model_revision` | `Qwen/Qwen3-0.6B` / `c1899de2…` | the pinned immutable revision |
| `tokenizer_chat_template_hash` | `a55ee1b1…` | the unmoved rendering semantics |
| `requested_steps` / `completed_steps` | 40 / **40** | the execution |
| `epochs_completed` | **2.0** | §8 |
| `train_loss` / `eval_loss` | 3.277057 / 3.01886 | §9 |
| `seed` / `precision` / `device_category` | 42 / `fp32` / `cpu` | the frozen policy |
| `package_versions` | torch 2.13.0+cpu · transformers 5.14.1 · peft 0.20.0 | the qualified runtime |
| `completed` / `interrupted` | `true` / `false` | |

### 10.3 Tensors

| | |
|---|---|
| Safetensors header parsed | yes |
| Tensor count | **392** |
| `lora_A` / `lora_B` tensors | **196 / 196** |
| Tensors that are not LoRA tensors | **0** |
| Adapted projections | `down_proj, gate_proj, k_proj, o_proj, q_proj, up_proj, v_proj` — all seven |
| dtype | `F32` only |
| Adapter parameter count | **10,092,544** |
| Trainable parameters (backend-reported) | **10,092,544** |
| Total model parameters | **606,142,464** |
| Trainable share | **1.665 %** |
| Non-finite tensors | **0 of 392** |
| All-zero tensors | **0** |

392 reconciles exactly: 28 layers × 7 projections × 2 matrices. The two independent
parameter counts — the safetensors header and the backend's own report — agree, and both
agree with the S3J.1 load-only prediction of 10,092,544 of 606,142,464.

**No base-model dump.** There is no `model.safetensors`, no `pytorch_model.bin` and no
non-LoRA tensor of any kind. 40.4 MB against a roughly 1.2 GB fp32 base model is the right
order of magnitude for r = 16 over seven projections.

**No pickle, and no checkpoint.** `.bin`, `.pt`, `.pth`, `.pkl` and `.pickle` are absent by
scan as well as by allowlist. `save_strategy` stayed `no` and `load_best_model_at_end`
stayed `False`, so evaluation ran while the trainer wrote no checkpoint at all.

### 10.4 Assistant-only loss, proven on both arms

```
TRAIN       strategy manual_label_masking(-100)   verified true   problems []
            probe: 35 prompt tokens masked · 66 supervised completion tokens
VALIDATION  strategy manual_label_masking(-100)   verified true   problems []
            probe: 26 prompt tokens masked · 97 supervised completion tokens
```

The masking self-test is a **refusal gate**, not a report: an unproven mask fails the run
rather than silently fitting the system and user turns.

### 10.5 Nothing escaped, and nothing private was written

The runs root gained exactly one directory, for this run id. The quarantine directory still
holds only the historical `qwen3-06b-lora-smoke-live-003-7e9b2593` residue, untouched. The
training ledger gained exactly two lines, `started` and `completed`, both bound to plan
`a07f9249…`.

A scan of every file in the run directory found **no absolute host path, no `/home/…`, no
`/Users/…`, no Windows user path, no cache location, no username and no raw dataset row**.
All of it is gitignored (`git check-ignore` confirms `training_runs/`), and none of it is
tracked.

---

## 11 — What this run does and does not establish

```
QUALITY_CANDIDATE_002:          TRAINED
QUALITY_CANDIDATE_002_EVALUATED: NO
QUALITY_CANDIDATE_002_ELIGIBILITY: UNKNOWN
MODEL_PROMOTION:                NOT_AUTHORIZED
MODEL_REGISTRY_MUTATED:         NO
```

**Established.** The second quality-oriented LoRA fine-tune ran to completion on real
weights, offline, on CPU/fp32, under a single-use authority spent exactly once; it consumed
exactly the corpus, exports, revision and hyperparameters S3J froze and S3J.1 qualified; it
completed all 40 predeclared optimizer steps at exactly 2.0 realised epochs; and it produced
a structurally valid, fully verified, LoRA-only adapter with a new identity. The S3J.1
load-only check is now extended by a real optimizer step — the thing §16.5 of that document
said only S3K could prove.

**Not established, and not to be claimed.** That the adapter is better than the baseline,
or better than candidate 001, at anything at all. No held-out material was touched, no
response was generated, no grader ran, no S3G §6 gate was evaluated, and `eval-v3` was
neither read as task text nor inferred against. A falling training loss means the optimiser
reduced the objective it was given on 154 rows; the validation curve is a twelve-row
diagnostic. **`CANDIDATE_STATUS` is `TRAINED_UNEVALUATED`, not `ELIGIBLE`.**

Every S3J and S3J.1 limitation survives this run intact.

---

## 12 — Limitations

1. **Candidate 002 is trained and unevaluated.** Everything about its quality is
   **unknown, not estimated**.
2. **The validation signal is twelve rows**, it is train-side steering material, it
   appears in no gate, and it is not comparable with candidate 001's nine-row number.
3. **S3K is one host, one seed, one run.** No repeat, no second seed, no second host, no
   ablation. `deterministic_reproduction_claimed` is `false`; no bit-reproducibility is
   claimed.
4. **The training corpus is synthetic and single-author** — 182 rows across two sessions
   by one author, sharing a process with the held-out corpus, so a systematic blind spot
   would be invisible to a comparison between them.
5. **The optimizer and scheduler actually used were transformers defaults**
   (`adamw_torch`, linear decay with the configured warmup ratio). They are not fields of
   `TrainingConfig`, so they are **not** pinned by the config hash and a future
   transformers upgrade could change them without moving any identity here.
6. **The Kali training runtime is not claimed bytewise equivalent** to the Windows runtime
   that produced candidate 001's adapter. The stack matches release for release; the OS,
   the interpreter and `numpy`'s patch digit do not.
7. **The adapter has never been loaded for inference.** Its 392 tensors are verified as
   bytes; no forward pass through the adapted model has been run by anything except the
   trainer's own teacher-forced evaluation arm.
8. **D28, D29 and D33 travel unchanged** into the future evaluation and bound what the
   tool-call, refusal and timeout metrics can mean.
9. **Semantic leakage checking has still never run.** All disjointness evidence is lexical
   and exact.
10. **The 512 qualification is bound to `m62-defensive-quality-train v2` at `24ceb1e0…`**
    with this tokenizer revision and this chat template. Change any of the three and it
    must be re-measured.

---

## 13 — Zero-evaluation, zero-promotion proof

```
LIVE_HELDOUT_EVALUATION:           NOT_RUN
EVAL_TOKEN_CREATED:                NO
EVAL_TOKEN_CONSUMED:               NO
EVAL_V3_STATUS:                    FROZEN_UNSEEN
EVAL_V3_REBUILT_OR_MODIFIED:       NO
EVAL_V3_INFERENCE_RUN:             NO
MODEL_RESPONSE_TOKENS_GENERATED:   0
GENERATION_PERFORMED:              NO   (recorded by the run's own evidence)
SMOKE_PROMPTS_RUN:                 0
S3I_RESCORED_OR_REPLAYED:          NO
CANDIDATE_001_MUTATED:             NO   (adapter re-hashed 43213035…)
GATE_POLICY_MODIFIED:              NO   (e5003319… reproduced)
SECOND_TRAIN_TOKEN:                NO
RETRY_ATTEMPTED:                   NO
HYPERPARAMETER_CHANGED_MID_RUN:    NO
EARLY_STOPPING:                    DISABLED
CHECKPOINT_SAVING:                 DISABLED
LOAD_BEST_MODEL_AT_END:            FALSE
MODEL_PROMOTION:                   NOT_AUTHORIZED
MODEL_REGISTRY_MUTATED:            NO
MERGE / TAG / RELEASE / VERSION_BUMP:  NO / NO / NO / NO
TRACKED_SOURCE_CHANGED:            NO
DEPENDENCIES_INSTALLED_OR_CHANGED: NO
EVALUATION_VENV_TOUCHED:           NO
```

---

## 14 — Tests and gates

**No tracked source changed in this milestone.** It is one live run plus documentation, so
per the brief's test policy the full suite was **not** re-run for ceremony — S3J.1 ran the
818-test focused and adjacent selection at the commit this run executed from. The bounded
checks that qualify this work were run instead.

| Gate | Result |
|---|---|
| Completed-run verification (`verify_completed_run`) | **PASS — 0 problems** over the bytes on disk |
| Safetensors / tensor finiteness / parameter reconciliation | **PASS** — 392 tensors, 0 non-finite, 0 all-zero, adapter param count equals the backend's trainable count |
| Artefact allowlist, checkpoint and forbidden-extension scan | **PASS** — 0 `checkpoint*`, 0 `.bin`/`.pt`/`.pth`/`.pkl`/`.pickle`, no base-model dump, no symlink, no nested directory |
| Dataset / export / cache re-verification before the token existed | **PASS** — every hash reproduced; `probe_cache` `present`; one revision cached |
| Plan reproduction (generator **and** production CLI, twice each) | **PASS** — `a07f9249…` every time, 0 blockers |
| Dependency gate (`TRAINING`, `SFT_LORA`) | **PASS** — `ready=True`, 0 blockers |
| Tokenizer / chat-template qualification | **PASS** — `a55ee1b1…` exact; 0 truncations of 166; masking verified both splits |
| Gate-policy drift | **PASS — zero drift**, `e5003319…` reproduced; QG-2 still absolute |
| Candidate 001 integrity | **PASS** — adapter re-hashed to `43213035…`, unchanged |
| `git diff --check` | **PASS** |
| Secret scan over the S3K changeset | **PASS** |
| Host-path scan over the changeset **and** the run artefacts | **PASS** — no absolute path, username, hostname or cache location in any tracked file or in any file the run wrote |
| `TRAIN:` / `EVAL:` token literal scan | **PASS** — no token literal in any tracked file |
| Runtime artefact exclusion | **PASS** — `git check-ignore` confirms `training_runs/`; the adapter, manifests and ledger are untracked |
| Ruff / Bandit / `compileall` | **NOT RUN — they gate tracked source changes; S3K has none.** Ruff and Bandit remain absent from this host |

---

## 15 — Exact NEXT

**M62 S3L — the second quality candidate's held-out eligibility evaluation.** Not
authorised in this session; it needs a new explicit operator authorisation and a **fresh
single-use `EVAL` authority**.

```
BIND     m62-defensive-eval v3, manifest 7c948236…, parent 82b60bfd…, pack 28d2f7d0…;
         Qwen/Qwen3-0.6B @ c1899de2…;
         candidate qwen3-06b-lora-quality-live-002, adapter 319c2524…;
         gate policy e5003319… UNCHANGED;
         reasoning_policy DISABLED; max_new_tokens 512;
         timeout_s 300 stated EXPLICITLY (the default is 120 s; D33 — declared, UNENFORCED);
         isolated_loads / PER_REQUEST, 36 + 36 = 72 loads.

RUN IN   the qualified Kali EVALUATION runtime .venv-m62-eval-linux — not the training
         venv. Offline: HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1,
         HF_HUB_DISABLE_TELEMETRY=1, local_files_only, trust_remote_code false.

DERIVE   the evaluation plan again in that session. Require 0 blockers. Never paste a
         previous plan hash in, and never force one.

SPEND    One EVAL: token, once. No retry. Then: no promotion, no activation, no registry
         mutation, no merge, no tag, no release, no version bump.
```

**Limitations that must be stated in that report:** D28 (`tool_call_validity_rate`
vacuous — the six `tool_call_schema` tasks cannot decide eligibility), D29 (phrase-list
refusal detection), D33 (`timeout_rate` structurally vacuous), the 36-task single-author
holdout, the 182-row single-author training corpus, lexical-only leakage evidence, and the
fact that the Kali runtime is not claimed bytewise equivalent to the Windows one.

---

## 16 — Final status

```
S3K_SECOND_QUALITY_LIVE_TRAINING: PASS
TRAINING_HOST:                    KALI_LINUX_MINI_PC
STARTING_HEAD:                    4ec4b36bcd4e012f68e07ce9a3737c475f270319

SECOND_CANDIDATE:                 qwen3-06b-lora-quality-live-002
CANDIDATE_PRE_TRAIN_STATUS:       DESIGNED_UNTRAINED
CANDIDATE_FINAL_STATUS:           TRAINED_UNEVALUATED

TRAIN_DATASET:                    m62-defensive-quality-train v2
TRAIN_V2_MANIFEST:                24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f
TRAIN_V2_PARENT:                  9bbac2f057fd0592a30a7fdeb968655f8ea585df00966e1b920415377ab7286a
TRAIN_EXPORT:                     82780fa0edc4c99198d0074a8a01b08507fa3eed54b4af50c3e045d5e07ae921
TRAIN_ROWS:                       154
VALIDATION_EXPORT:                ac065112c4cb3a2195100c3f11289d1e109f40441d293ded280d9b6cddd540fd
VALIDATION_ROWS:                  12

TRAINING_VENV:                    .venv-m62-train-linux
PYTHON / TORCH:                   3.13.14 / 2.13.0+cpu
TRANSFORMERS / PEFT:              5.14.1 / 0.20.0
DATASETS / TRL / ACCELERATE:      5.0.1 / 1.9.2 / 1.14.0
DEVICE / PRECISION / CUDA:        CPU / FP32 / False

BASE_MODEL:                       Qwen/Qwen3-0.6B
BASE_REVISION:                    c1899de289a04d12100db370d81485cdf75e47ca
CHAT_TEMPLATE_DIGEST:             a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8
TRUNCATIONS:                      0 / 166

LORA_R / ALPHA / DROPOUT:         16 / 32 / 0.05
TARGET_MODULES:                   q,k,v,o,gate,up,down   (all seven adapted)
LEARNING_RATE:                    1e-4
EPOCHS:                           2
BATCH / GRAD_ACCUM / EFFECTIVE:    1 / 8 / 8
SEED / MAX_SEQ:                   42 / 512
CONFIG_HASH:                      08be37d37dd403ea8b049ab7bb32498f5d767ef013876920783ad4669e608649

LIVE_PLAN_HASH:                   a07f924969387e2b42db5e86d98f1f438d464f94bc969e79f9a0f194f790ffcb
PLAN_BLOCKER_COUNT:               0
PLAN_WARNINGS:                    1   (CPU-run caution)
PLAN_REDERIVED_NOT_FORCED:        YES  (738b187f… and f7209a64… used as REFERENCE ONLY)

TRAIN_TOKEN_CREATED:              YES
TRAIN_TOKEN_CONSUMED:             YES  (exactly once, 2026-08-14T23:34:11Z)
TRAIN_ATTEMPTS:                   1
RETRY_AUTHORIZED:                 NO

TRAINING_RESULT:                  SUCCESS
START / END (UTC):                2026-08-14T23:34:11Z -> 2026-08-14T23:49:43Z
WALL_TIME:                        15m32s  (932 s; backend 930.196 s)
OPTIMIZER_STEPS_COMPLETED:        40 / 40
REALIZED_EPOCHS:                  2.0
TRAIN_LOSS:                       3.277057
TRAIN_LOSS_CURVE:                 3.952389 -> 2.925896 over 8 logged points (not monotone)
PERIODIC_VALIDATION_EVALUATIONS:  2   (epoch 1.0 step 20; epoch 2.0 step 40)
PERIODIC_VALIDATION_LOSSES:       3.090760 / 3.018860
FINAL_VALIDATION_EVALUATION:      PRESENT  (closing evaluate(), 11.5471 s)
VALIDATION_LOSS_FINAL:            3.018860
VALIDATION_ROWS_TRUNCATED:        0 / 12
NON_FINITE_METRIC_DETECTED:       NO
EARLY_STOPPING:                   DISABLED
CHECKPOINT_SAVING:                DISABLED
LOAD_BEST_MODEL_AT_END:           FALSE

ADAPTER_CREATED:                  YES
ADAPTER_FILE:                     adapter_model.safetensors
ADAPTER_SIZE_BYTES:               40422168
ADAPTER_SHA256:                   319c252498ba51e01ed59f58fc20ae639e2d886bf67277d3aa6df2e9f9665409
ADAPTER_MANIFEST_HASH:            11897e16b081cc4df2517f1c0c0904b7b7580ab4daf8fea0157e49ee4e2f6ca8
ARTIFACT_SET_TREE_HASH:           220350efe5e2dda594f17ca03f1cf6db15885403a908a3b6dcd899b4d498d6f4
LORA_TENSOR_COUNT:                392   (196 lora_A + 196 lora_B)
NON_LORA_TENSOR_COUNT:            0
ALL_TENSORS_FINITE:               YES   (0 of 392 non-finite)
ALL_ZERO_LORA_TENSORS:            0
TRAINABLE_PARAMETERS:             10092544
TOTAL_PARAMETERS:                 606142464
TRAINABLE_SHARE:                  1.665%
SAFETENSORS_ONLY:                 YES
CHECKPOINT_DIRECTORIES:           0
FORBIDDEN_ARTIFACTS:              0
BASE_MODEL_DUMP_DETECTED:         NO
COMPLETED_RUN_VERIFIER:           PASS  (0 problems)

CANDIDATE_001:                    UNTOUCHED  (43213035…, EVALUATED_NOT_ELIGIBLE)
GATE_POLICY_HASH:                 e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5
EVAL_V3:                          FROZEN_UNSEEN
EVAL_TOKEN_CREATED:               NO
LIVE_EVALUATION:                  NOT_RUN
MODEL_RESPONSE_TOKENS_GENERATED:  0
MODEL_PROMOTION:                  NOT_AUTHORIZED
MODEL_REGISTRY_MUTATED:           NO
MERGE / TAG / RELEASE / VERSION_BUMP:  NO / NO / NO / NO
SOURCE_CHANGED:                   NO
```
