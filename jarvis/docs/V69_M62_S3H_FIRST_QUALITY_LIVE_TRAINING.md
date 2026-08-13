# V69 M62 S3H — First quality-oriented live training run

**Date (UTC):** 2026-08-13
**Status:** TRAINING RUN COMPLETE — **CANDIDATE NOT EVALUATED**
**Scope:** consume one single-use `TRAIN:` authority and execute the qualified S3G/S3G.1/S3G.2
candidate once, on real weights, offline, on CPU; observe train and validation loss; verify the
resulting adapter. Nothing was evaluated against held-out material, nothing was promoted, no
registry was touched.

```
S3H_LIVE_TRAINING:        PASS
TRAINING_EXECUTED:        YES
TRAIN_TOKEN_CREATED:      YES
TRAIN_TOKEN_CONSUMED:     YES
TRAIN_ATTEMPTS:           1
ADAPTER_CREATED:          YES
CANDIDATE_STATUS:         TRAINED_UNEVALUATED
LIVE_HELDOUT_EVALUATION:  NOT_RUN
MODEL_REGISTRY_MUTATED:   NO
MODEL_PROMOTION:          NOT_AUTHORIZED
```

This document does **not** revise S3G, S3G.1 or S3G.2. Those three milestones designed,
qualified and wired the candidate and each closed with `TRAINING_EXECUTED: NO`. That remains
the honest record of them. What changed here is that the operator authorised **one** live
attempt and it was taken.

---

## 1. Authorisation and boundary

The human operator explicitly authorised **M62 S3H — first quality-oriented live training
run**: exactly **ONE** training attempt for `qwen3-06b-lora-quality-live-001` under the
qualified S3G.2 plan, using the operator-supplied reviewed model cache, with train-side
validation as a diagnostic eval arm, followed by artefact verification, documentation, a
PROGRESS update, commits and a push.

It authorises **none** of: a second training attempt, automatic retry after failure, token
reuse, fresh TRAIN authority after a consumed token, any change to the dataset,
hyperparameters, LoRA configuration or `max_sequence_length`, checkpoints, early stopping,
`load_best_model_at_end=True`, held-out model evaluation, creating or consuming an `EVAL`
token, evaluation against `m62-defensive-eval` v1 or v2, generation for quality evaluation,
promotion, activation, Model Registry mutation, merge, tag, release, version bump, or any
work on run-004. Every one of those remains not done.

**Starting checkpoint (verified, not assumed):** branch `jarvis-v69-m62-training-gym`, HEAD
`a167420f831c9359152a147d8cf12c40cebc8434`, `origin/…` divergence `0 0`, `origin/master`
`3705114228edef2f665be349c5c4429b7b16777a` unchanged, working tree clean.

**Subagents: none.** Every identity below was reproduced by the main agent directly against
repository authority, which the brief makes primary over any subagent opinion.

---

## 2. Candidate

```
RUN_ID:            qwen3-06b-lora-quality-live-001
EXPERIMENT_NAME:   m62-s3g-defensive-quality-001
RUN_INTENT:        QUALITY_CANDIDATE
```

**It is not run-004 and it never touched it.** Operator ruling H5 stands unchanged:
`qwen3-06b-lora-smoke-live-004` remains `KEEP_AS_SMOKE_REFERENCE_ONLY` and
`EXCLUDED_FROM_QUALITY_PROMOTION`. Nothing here resumed, continued, copied, mutated,
retrained or read it.

---

## 3. Pre-token verification

Everything below was re-derived this session through the repository's own authorities, before
any authority was derived and before anything was spent. No value was quoted from a document
and accepted.

| Item | Expected (S3G / S3G.1 / S3G.2) | Measured this session |
|---|---|---|
| Dataset manifest hash | `9bbac2f057fd0592a30a7fdeb968655f8ea585df00966e1b920415377ab7286a` | identical |
| Promoted rows | 128 | 128 |
| Split counts | TRAIN 107 · VALIDATION 9 · HIDDEN_EVALUATION 6 · SECURITY_REGRESSION 6 | identical |
| Split policy hash | `1c8b242a379dbe43cf58877f7d65e2bae77b797170c5655b5787caabf97df842` | identical |
| Internal leakage report hash | `535f37bbcc9604cee8b1faec0e537cb748d28e83d2e7cf1381ba1deecc8f1684` | identical |
| SFT **train** export hash | `b785e7135441c406efcee94d71a8e83965758de22a70be0d575612128bb3dc4a` | identical, 107 rows, 0 excluded, verifies |
| SFT train export file sha256 | `83f629041eeabb6e9df9ab999f2e9c7d7d469074362ece82717f3318827032e4` | identical |
| SFT **validation** export hash | `589e056baff10690a58fca37b34d78612ea0c7ed0387a7a294fc27f05d978606` | identical, 9 rows, 0 excluded, verifies |
| Validation export file sha256 | `7a1429ddfb0d31e3a38b1936e8e95ffb3d7496e5f2566a5151da6cdfb3630b79` | identical |
| Dataset reference hash | `1f4cdc6f7f6bdd4da18d179da1afe79bf72169b08de8a7c1f7afb42ff6d0e211` | identical |
| Reviewed cache root digest | `40f747d2037e389b` | identical |
| `probe_cache` verdict / evidence digest | `present` / `f399355ef441e8ec` | identical |
| Revisions in the cache | exactly `c1899de289a04d12100db370d81485cdf75e47ca` | exactly that one |
| Chat template digest | `a55ee1b1660128b7` | identical |
| TRAIN max full sample / truncated at 512 | 169 / 0 | 169 / 0 |
| VALIDATION max full sample / truncated at 512 | 150 / 0 | 150 / 0 |
| `output_root_id` | `56bb1a6e85d39398…` | identical |
| **Config hash** | **`b5f63cd8f65c7bc91c52b58b1d53a18bc757ff361d59f83b98e33f7a1dcafb03`** | **identical** |
| **Plan hash** | **`122efc62491256b25756eb24be37d3695347763295682f7409ea231293507ffe`** | **identical** |
| Plan blockers / warnings | 0 / 2 | 0 / 2 |

**The absolute cache path is not recorded here**, only its digest — the same convention S3G.1
and S3G.2 used, and the digest is what identifies it as the same reviewed cache without any
document naming the location.

**The bounded length check was a consistency check, not a repeat of the S3G.1 audit.** Both
train-side splits were re-encoded through the production `TransformersPeftBackend._encode`
against the real pinned tokenizer at the real 512 cap, and both reproduced S3G.1 and S3G.2
exactly. The 128-row audit was not re-run; nothing that binds it moved.

**The plan was reproduced twice, by two callers**: once through the tracked generator
`jarvis/scripts/build_quality_training_config.py --plan`, and once through the production
`jarvis/scripts/train_experiment.py --print-plan`, which is the authority `--execute` itself
recomputes against. Both returned `122efc62…`, `is_executable: true`, `blockers: []` and the
same two pre-existing warnings (the M17 memory cross-check disagreement and the standing CPU
caution). Neither was suppressed. Both runs left the runs root empty and the ledger at two
lines.

### 3.1 The pre-token gate

```
S3H_PRETOKEN_GATE:            PASS
GIT_CLEAN:                    YES
DATASET_HASH_MATCH:           YES
TRAIN_EXPORT_VERIFIED:        YES
VALIDATION_EXPORT_VERIFIED:   YES
MODEL_CACHE_VERIFIED:         YES
MODEL_REVISION_MATCH:         YES
TOKENIZER_TEMPLATE_MATCH:     YES
MAX_SEQUENCE_LENGTH_512:      QUALIFIED
CONFIG_HASH_MATCH:            YES
PLAN_HASH_MATCH:              YES
PLAN_BLOCKERS:                0
CHECKPOINTS_DISABLED:         YES
EARLY_STOPPING_DISABLED:      YES
LOAD_BEST_MODEL_AT_END_FALSE: YES
OFFLINE:                      YES
```

---

## 4. Model and execution policy

| | |
|---|---|
| Base model | `Qwen/Qwen3-0.6B` |
| Immutable revision | `c1899de289a04d12100db370d81485cdf75e47ca` |
| Tokenizer | same id, same revision |
| Cache | operator-supplied reviewed cache, `present`, digest `40f747d2037e389b` |
| Offline | `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `HF_HUB_DISABLE_TELEMETRY=1` |
| `local_files_only` | `true` (recorded in the run's own evidence) |
| `trust_remote_code` | `false` (recorded in the run's own evidence) |
| Download policy | `deny`; `downloaded_anything: false` |
| Device / precision | **CPU** / **FP32** |
| Interpreter | the isolated `.venv-training-smoke` — torch 2.13.0+cpu, transformers 5.14.1, peft 0.20.0 |

Nothing was downloaded, installed, uploaded or contacted. No teacher, no cloud, no network.

---

## 5. Configuration

Option B, byte for byte as S3G designed it and S3G.2 re-identified it. **No hyperparameter was
changed for this run.**

| | Value |
|---|---|
| Method | `SFT_LORA` |
| LoRA rank / alpha / dropout / bias | 16 / 32 / 0.05 / none |
| Target modules | `q_proj, k_proj, v_proj, o_proj, gate_proj, up_proj, down_proj` |
| Task type | `CAUSAL_LM` |
| Learning rate / weight decay / warmup | 2e-4 / 0.0 / 0.1 |
| Epochs declared / `max_steps` | 3 / 40 |
| Batch × grad-accum = effective | 1 × 8 = 8 |
| `max_sequence_length` | 512 |
| Seed | 42 |
| Optimizer / scheduler | `adamw_torch` / linear decay — **backend defaults, not fields of this schema** |
| `checkpoint_strategy` / `gradient_checkpointing` | `no` / `false` |
| `validation_strategy` / `validation_split` | `epoch` / `validation` |
| Early stopping / `load_best_model_at_end` | disabled / `false` |
| Config hash | `b5f63cd8f65c7bc91c52b58b1d53a18bc757ff361d59f83b98e33f7a1dcafb03` |

---

## 6. TRAIN authority

```
TOKEN_FORM:            TRAIN:<plan-hash>
BOUND_PLAN_HASH:       122efc62491256b25756eb24be37d3695347763295682f7409ea231293507ffe
TOKENS_DERIVED:        1
TRAIN_TOKEN_CREATED:   YES
TRAIN_TOKEN_CONSUMED:  YES  (exactly once, 2026-08-13T21:57:26Z)
TRAIN_ATTEMPTS:        1
```

The token is **derived** from the plan by `TrainingPlan.confirmation_token()`, never issued by
one; computing a plan spends nothing. Exactly one was derived, by the main agent, and handed
to no subagent and to no second process. **The reusable string is deliberately not written
into any tracked file** (PROGRESS §17) — what is recorded is the plan hash it binds, which is
already public in this document, and the consumption state.

Consumption is proven by the run's own artefacts rather than asserted: `training_log.jsonl`
carries `plan_consumed` in state `starting`, the CLI outcome reports `plan_consumed: true`,
and the ledger gained exactly one `started` line for this run id against this plan hash. It
can never return to `NO`, and no second attempt is authorised whatever the outcome had been.

---

## 7. Execution

```
TRAINING_RESULT:  SUCCESS
```

| | |
|---|---|
| Started (UTC) | `2026-08-13T21:57:25Z` |
| Ended (UTC) | `2026-08-13T22:25:12Z` |
| Wall clock | **27 min 47 s** (1667 s) |
| Backend-reported duration | 1662.828 s |
| Hard operator ceiling | 4 hours — **not approached** |
| Optimizer steps planned / attempted / completed | 40 / 40 / **40** |
| Realised epochs | **2.897196** |
| Converted training records | 107 |
| Truncated records | **0** |
| Backend status / warnings | `succeeded` / none |
| Run state | `completed`, `interrupted: false`, `error_category: none` |
| States visited | `config_validated → dataset_verified → dependencies_verified → hardware_verified → planned → awaiting_confirmation → preflight_verified → starting → running → artifact_validation → completed` |

**The runtime estimate held.** S3G.2 predicted 20–52 minutes including validation overhead;
the run took 27m47s, inside the range and well inside it. This is the **first measured per-run
timing on this host** — the cost model of S3G §9.2 gains a second calibration point, though
still only one for a run of this shape.

**Realised epochs are 2.897, not 3.** `max_steps=40` bounds the run: 40 × 8 ÷ 107 = 2.9907
declared, and the trainer reports 2.897196. That is the expected behaviour S3G described, not
a shortfall.

---

## 8. Losses

### 8.1 Training loss

Aggregate reported by the trainer: **`train_loss = 2.991393`**.

The logged curve, at the configured 5-step logging interval:

| step | epoch | training loss |
|---:|---:|---:|
| 5 | 0.373832 | 4.100562 |
| 10 | 0.747664 | 3.262878 |
| 15 | 1.074766 | 3.135795 |
| 20 | 1.448598 | 2.892009 |
| 25 | 1.822430 | 2.812190 |
| 30 | 2.149533 | 2.659771 |
| 35 | 2.523364 | 2.564753 |
| 40 | 2.897196 | 2.503183 |

Monotonically decreasing at every logged point, 4.100562 → 2.503183. No non-finite value at
any step.

### 8.2 Validation loss

```
VALIDATION_STRATEGY:               EPOCH_PLUS_FINAL
PERIODIC_VALIDATION_EVALUATIONS:   3
FINAL_VALIDATION_EVALUATION:       PRESENT
VALIDATION_ROWS:                   9
VALIDATION_ROWS_TRUNCATED:         0
```

Periodic (`eval_strategy="epoch"`):

| # | epoch | global step | eval loss | eval runtime (s) |
|---:|---:|---:|---:|---:|
| 1 | 1.000000 | 14 | 3.205301 | 16.5102 |
| 2 | 2.000000 | 28 | 3.122892 | 14.1540 |
| 3 | 2.897196 | 40 | 3.125407 | 14.7902 |

Closing explicit `trainer.evaluate()`, recorded separately as `final_evaluation` with
`at_end_of_training: true`:

| | epoch | eval loss | eval runtime (s) |
|---|---:|---:|---:|
| **FINAL_VALIDATION_LOSS** | 2.897196 | **3.125407** | 18.4869 |

**One prediction of S3G.2 §6 needs correcting forward, and it is worth being exact.** S3G.2
reasoned that because `max_steps=40` stops the run at 2.99 realised epochs, *"there may be no
third boundary to fire at"*, so the last periodic measurement would be the end of epoch 2. On
this transformers build that is not what happened: a third periodic evaluation **did** fire, at
step 40 / epoch 2.897196, i.e. at the end of training. The closing pass therefore measured the
same weights as the third periodic one and returned the same loss to every recorded digit —
`3.125407` — while taking a visibly different 18.4869 s against 14.7902 s, which is direct
evidence that they are two separate forward passes and not one number recorded twice.

**This does not make the closing pass redundant, and it must not be removed.** Its purpose is
to guarantee an end-of-run number *whatever* the cadence does — and the cadence's behaviour at
a fractional final epoch is a property of the installed transformers build, not of this
repository. The two remain recorded separately, as designed; neither is presented as the other.

### 8.3 What the two curves say, and what they do not

Training loss fell 4.100562 → 2.503183 across the run. Validation loss fell 3.205301 →
3.122892 between epochs 1 and 2, then moved **up** by 0.002515 to 3.125407 at the end.

Read carefully, that is the beginning of the divergence pattern S3G §10.3 named as this
candidate's most likely failure mode: the training loss continues down while the validation
loss flattens and turns. **It is exactly what the S3G.2 wiring exists to make visible**, and
before D31 was fixed this run would have reported a single training loss and `eval_loss: 0.0`.

It is also, on its own terms, a **weak** signal and is not being inflated into anything:

- nine rows (PROGRESS §14.33) cannot rank two runs; a 0.0025 movement over nine examples is
  well inside what sampling noise on that denominator can produce;
- validation is **train-side steering material** by this repository's `TRAIN_SIDE_SPLITS`
  definition — it is not held-out evidence;
- it contributed no gradient, selected no weights, stopped nothing and wrote no checkpoint;
- **it is not a quality score.** It says nothing about refusal behaviour, structured output,
  over-refusal or security, and it appears in no S3G §6 acceptance gate.

```
NON_FINITE_METRIC_DETECTED:  NO
```

Every logged training loss, every eval loss, the aggregate train loss and the final eval loss
are finite. No `NaN`, no `±inf`, at any point.

### 8.4 Validation hygiene, from the run's own evidence

| Property | Recorded value |
|---|---|
| `enabled` / `strategy` / `split` | `true` / `epoch` / `validation` |
| `contributes_gradients` | **`false`** |
| `early_stopping` | **`false`** |
| `load_best_model_at_end` | **`false`** |
| `generation_performed` | **`false`** |
| `is_held_out_eligibility_evidence` | **`false`** |
| `validation_rows` / `validation_rows_truncated` | 9 / **0** |
| Eval-arm masking self-test | `verified: true`, `problems: []`, 26 prompt / 97 completion tokens on the probe |
| Train-arm masking self-test | `verified: true`, `problems: []`, 35 prompt / 66 completion tokens on the probe |

Both arms passed the production `_masking_self_test`, so the validation loss is an
assistant-only loss and is comparable to the training loss it is read against.

---

## 9. Artefacts

```
ADAPTER_CREATED:            YES
ARTIFACT_VERIFICATION:      PASS
SAFETENSORS_ONLY:           YES
CHECKPOINT_DIRECTORIES:     0
FORBIDDEN_ARTIFACTS:        0
BASE_MODEL_DUMP_DETECTED:   NO
```

### 9.1 The run directory

Flat — no nested directory, no symlink, no `checkpoint-*` entry.

| File | bytes | sha256 |
|---|---:|---|
| `adapter_model.safetensors` | 40,422,168 | `43213035c15cd38928d2d6a3bdbd9af96872a954801c6bfd0a9b82a8e22ac858` |
| `adapter_config.json` | 1,197 | `9a3eddb2df8ab3337d207236bbab7081151837d60c4232bb3596a76e24a89639` |
| `README.md` (the model card PEFT writes; D15) | 5,184 | `9bd3cee25f0a5d53eb86d621dfb4c893f037bf340cee6c3f580a517bec928061` |
| `backend_result.json` | 2,836 | `11c17c457870f79a8339fc6895bdb42260f084b186ad237a5231b19452824c09` |
| `run.json` | 1,620 | `c194e69cd50fb9ae359de1f8c90f1dc0542803d5bfc24c0e1e6191331c171f16` |
| `training_log.jsonl` | 827 | `2a6bce087d50970ac80f2388d4b200bf9c4f97ede8136cbfb252ca55fd8925fd` |
| `adapter-manifest.json` | 3,591 | the manifest itself; digested as `manifest_hash` below |

| Digest | Value |
|---|---|
| Adapter manifest hash | `1f76ccfbb8efc566c293ab6430d041dd24748035ed48aec6552d1e3bac24699f` |
| Artifact-set (tree) hash | `00aa57bbbe7f0af73501dae2330fb0b08682ede813843f92b26681ec77d659b6` |
| Run record hash | `66f0839d1af6bd0e8661855d6bdb44becb3a4ac61ef9dab3429fc822b6c1b5c2` |

### 9.2 Verification

`training_gym.training.artifacts.verify_completed_run` — the repository's own authority, not a
second opinion — returned **no problems**. It re-derives every claim the directory makes: the
manifest parses and its own digest matches, every file it names is present and rehashes to the
recorded digest and size, **no file is present that it does not name**, and the tree digest
re-derives from the bytes on disk.

The manifest binds the run to its authorities, and each was checked against the value this
session verified before training:

| Manifest field | Value | Binds to |
|---|---|---|
| `run_id` | `qwen3-06b-lora-quality-live-001` | the authorised candidate |
| `plan_hash` | `122efc62…` | the qualified S3G.2 plan |
| `training_config_hash` | `b5f63cd8…` | the validation-enabled config identity |
| `dataset_manifest_hash` | `9bbac2f0…` | `m62-defensive-quality-train v1` |
| `base_model_id` / `base_model_revision` | `Qwen/Qwen3-0.6B` / `c1899de2…` | the pinned immutable revision |
| `steps_completed` / `epochs_completed` | 40 / 2.897196 | the execution |
| `train_loss` / `eval_loss` | 2.991393 / 3.125407 | §8 |
| `completed` | `true` | |

**`eval_loss` carries a real measurement for the first time.** Before D31 it was the dataclass
default `0.0` on every run — a field that always reads zero looks like a measurement. This is
the first adapter manifest in this repository whose `eval_loss` is one.

### 9.3 Tensors

| | |
|---|---|
| Safetensors header parsed | yes |
| Tensor count | **392** |
| `lora_A` / `lora_B` tensors | 196 / 196 |
| Tensors that are not LoRA tensors | **0** |
| Adapted projections | `down_proj, gate_proj, k_proj, o_proj, q_proj, up_proj, v_proj` |
| dtype | `F32` only |
| Adapter parameter count | **10,092,544** |
| Trainable parameters (backend-reported) | **10,092,544** |
| Total model parameters | 606,142,464 |
| Non-finite tensors | **0 of 392** |
| All-zero tensors | **0** |

392 reconciles exactly: 28 layers × 7 projections × 2 matrices. It is the same count run-004
produced through the `all-linear` sentinel, reached here from an explicitly named module list
— which is what makes D15's exact-match rule a real check rather than an approval of whatever
the sentinel resolved to. The two independent parameter counts agree, and 10,092,544 of
606,142,464 is 1.665% trainable.

**No base-model dump.** There is no `model.safetensors`, no `pytorch_model.bin` and no
non-LoRA tensor of any kind. 40.4 MB against a roughly 1.2 GB fp32 base model is the right
order of magnitude for r=16 over seven projections, and matches S3G's ~38 MB estimate.

**No pickle, and no checkpoint.** `.bin`, `.pt`, `.pth`, `.pkl` and `.pickle` are absent by
scan as well as by allowlist. `save_strategy` stayed `no` and `load_best_model_at_end` stayed
`False`, so evaluation ran while the trainer wrote no checkpoint at all — the coupling S3G.2
§7 argued for, now demonstrated by a real run rather than by constructed objects.

### 9.4 Nothing escaped, and nothing private was written

The runs root gained exactly one directory, for this run id. The quarantine directory still
holds only the historical `qwen3-06b-lora-smoke-live-003-7e9b2593` residue from 2026-08-05,
untouched. The training ledger gained exactly two lines, `started` and `completed`, both
bound to plan `122efc62…`.

A scan of every file in the run directory found **no absolute host path, no Windows user
path, no `/home/…`, no `/Users/…`, no cache location, no username and no raw dataset row**.
All of it is gitignored (`git check-ignore` confirms `training_runs/`), and none of it is
tracked.

---

## 10. What this run does and does not establish

```
QUALITY_CANDIDATE:              TRAINED
QUALITY_CANDIDATE_EVALUATED:    NO
QUALITY_CANDIDATE_ELIGIBILITY:  UNKNOWN
MODEL_PROMOTION:                NOT_AUTHORIZED
MODEL_REGISTRY_MUTATED:         NO
```

**Established.** A quality-oriented LoRA fine-tune of the qualified corpus ran to completion
on real weights, offline, on CPU, under a single-use authority spent exactly once; it produced
a structurally valid, fully verified LoRA-only adapter; its training loss fell monotonically
and its validation loss was observed at three epoch boundaries plus an explicit closing pass.
The D31 wiring, the D30 blocker fix and the checkpoint-safety argument have now all been
exercised by a live training run for the first time.

**Not established, and not to be claimed.** That the adapter is better than the baseline at
anything. No held-out material was touched, no response was generated, no grader ran, no gate
in S3G §6 was evaluated. A falling training loss means the optimiser reduced the objective it
was given on 107 rows; the flattening validation loss is a nine-row diagnostic. Neither is
quality evidence, and **`CANDIDATE_STATUS` is `TRAINED_UNEVALUATED`, not `ELIGIBLE`.**

Every S3G, S3G.1 and S3G.2 limitation survives this run intact: the corpus is synthetic and
single-author, 107 training rows is small, semantic leakage checking has never run, the gates
are counts rather than calibrated thresholds, D29 bounds what a rise in
`required_refusal_success_rate` would mean, and D28 leaves tool calls unmeasured
(`TOOL_CALL_CAPABILITY: NOT_QUALIFIED`).

---

## 11. What was NOT done

- No second training attempt, no retry, no resume, no re-plan. One attempt, one token, one
  adapter.
- No held-out evaluation. `m62-defensive-eval` v1 and v2 were not read, not planned against
  and not generated against.
- No `EVAL` plan and no `EVAL` token created or consumed.
- No generation of any kind — the validation arm is teacher-forced loss only.
- No early stopping, no checkpoint, no `load_best_model_at_end`, no best-weight selection.
- No promotion, activation, registry mutation, role assignment or adapter merge.
- No mutation of run-004, the S3E.2 artefacts, `m62-defensive-eval` v1 or v2, the training
  corpus, or any historical result.
- No merge, tag, release or version bump.
- No hyperparameter, dataset, LoRA or `max_sequence_length` change.
- No dependency installed, no global environment change, no network contact, no download.
- No filesystem sweep for the model cache — the operator supplied it.
- No absolute host path recorded in any tracked file.
- No tracked source changed.

---

## 12. Tests and gates

**No tracked source changed in this milestone.** It is one live run plus documentation, so per
the brief's test policy the 6701-test suite was **not** re-run for ceremony — S3G.2 ran the
authoritative suite immediately before this run, at the commit this run executed from. The
bounded checks that qualify this work were run instead:

| Check | Result |
|---|---|
| Dataset version re-verification (`verify_version`, re-hashes every shard) | PASS — 0 problems |
| Train export verification (`verify_sft_export`) | PASS — 107 rows, hash unmoved |
| Validation export verification (`verify_sft_validation_export`) | PASS — 9 rows, hash unmoved |
| Reviewed cache verification (`probe_cache`) | PASS — `present`, one revision |
| Bounded tokenizer/template + truncation consistency check | PASS — template digest unmoved, 0 of 116 train-side rows truncate |
| Plan reproduction, generator | PASS — `122efc62…`, 0 blockers |
| Plan reproduction, production CLI (`--print-plan`) | PASS — identical, and it created nothing |
| Live training execution | PASS — `completed`, exit 0 |
| Completed-run verification (`verify_completed_run`) | PASS — **0 problems** |
| Safetensors header / tensor finiteness / parameter reconciliation | PASS — 392 tensors, 0 non-finite, counts agree |
| Artefact allowlist, checkpoint and forbidden-extension scan | PASS — 0 checkpoints, 0 forbidden files, no base dump |
| `git diff --check` | PASS |
| Secret scan (`core.redaction_policy.scan_for_leaks`) over the changeset | **PASS**, with the findings named rather than suppressed — see below |
| Host-path scan over the changeset | PASS — no absolute host path, no Windows user path, no cache location, no username in either changed file |
| Token scan over tracked files | PASS — the literal `TRAIN:<64-hex>` string appears in no tracked file |
| Scan of the files the run itself wrote | PASS — no absolute path, no username, no cache location, no raw dataset row |
| Runtime artefact exclusion | PASS — `git check-ignore` confirms `training_runs/`; nothing runtime is tracked |

**The two scanner findings, stated exactly.** `scan_for_leaks` returns one `reasoning`
category over `PROGRESS.md` and one `home_path` category over each changed file. Neither is
new material:

- **`reasoning`** — every hit is the literal `<think>` inside prose *describing* D24/D26a or
  inside the invariant check that forbids it. Identical to what S3G and S3G.2 recorded, and
  operator ruling **H4** classifies reasoning markup as hygiene, not a security leak.
- **`home_path`** — the hits are the sentences that *assert the absence* of such a path:
  this document's own §9.4 line, and the pre-existing S3G.1/S3G.2 gate rows in PROGRESS §15,
  each of which contains the literal `/home/…` in order to say it is not there.

Neither was reworded to make the scanner quiet. A detector that fires on the sentence
announcing its own cleanliness is the same false-positive shape S3G already recorded for
`<think`, and hiding it would cost more than reporting it.

Ruff, `compileall` and Bandit gate tracked source changes; there are none.

---

## 13. Limitations

1. **The candidate is unevaluated.** Everything about its quality is unknown, not estimated.
2. **Nine validation rows is a very small sample**, and the 0.0025 uptick at the end is well
   inside sampling noise on that denominator. It is enough to see a gross divergence; it is
   not enough to rank two runs, and it is not eligibility evidence.
3. **One host, one seed, one run.** No repeat, no second seed, no second host, no ablation.
   `deterministic_reproduction_claimed` is `false` and nothing here claims bit-reproducibility.
4. **The compute model now has two calibration points, not many.** 27m47s is one measurement
   of one configuration on one machine.
5. **Optimizer and scheduler were the installed transformers defaults** (`adamw_torch`, linear
   decay with the configured warmup). They are not fields of `TrainingConfig`, so they are an
   observation about the backend, not a choice this run controlled — and they are therefore
   not pinned by the config hash.
6. Every S3G / S3G.1 / S3G.2 limitation survives (§10), including D28, D29, the lexical-only
   leakage check and the uncalibrated thresholds.
7. **The adapter has never been loaded for inference.** Its tensors are verified as bytes; no
   forward pass through the adapted model has been run by anything other than the trainer's
   own evaluation arm.

---

## 14. Next

**M62 S3I — first quality-candidate held-out eligibility evaluation.** Under the contract
S3G §12 fixed and against the gates S3G §6 predeclared *before any of this ran*:
`m62-defensive-eval v2` (`10ad2308…`), baseline `Qwen/Qwen3-0.6B` @ `c1899de2…` with no
adapter versus the same model plus this adapter, `reasoning_policy = DISABLED`,
`max_new_tokens = 512`, body-free review evidence, security as a veto, tool calls reported
`VACUOUS` with `TOOL_CALL_CAPABILITY: NOT_QUALIFIED`, and a **fresh** `EVAL` plan and
single-use token consumed exactly once.

It requires **explicit operator authorisation, which has not been given.**

**S3I is not started, and must not begin automatically.**

**NO HELD-OUT EVALUATION WAS PERFORMED. NOTHING WAS PROMOTED.**
