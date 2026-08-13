# V69 M62 S3G.1 — Final pre-train qualification: reviewed cache, zero-blocker plan, real tokenizer audit

**Date (UTC):** 2026-08-13
**Status:** QUALIFICATION COMPLETE — **TRAINING NOT RUN**
**Scope:** verify the operator-supplied reviewed model cache, measure the qualified training
corpus with the *real* pinned tokenizer, rebuild the recommended configuration and its plan
to zero blockers, and state S3H readiness. Nothing was trained, nothing was generated, no
token was created and none was consumed.

```
S3G1_PRETRAIN_QUALIFICATION: PASS
TRAINING_EXECUTED:           NO
ADAPTER_CREATED:             NO
TRAIN_TOKEN_CREATED:         NO
TRAIN_TOKEN_CONSUMED:        NO
MODEL_REGISTRY_MUTATED:      NO
```

This document does **not** revise S3G. S3G closed with an unverified cache and estimated
token counts, and that remains the honest record of it. What changed here is an **input** —
the operator supplied the reviewed cache root — and a **measurement** that S3G could not
take without it.

---

## 1. Authorisation and boundary

The operator authorised **M62 S3G.1 — final pre-train qualification** only: cache
verification, the tokenizer length audit, configuration and plan rebuild, documentation,
PROGRESS update, commit and push.

It authorises **none** of: live training, `trainer.train()`, consuming a `TRAIN` token,
adapter creation, model generation, live inference, live evaluation, consuming an `EVAL`
token, promotion, activation, Model Registry mutation, merge, tag, release or version bump.
Every one of those remains not done.

**Starting checkpoint (verified, not assumed):** branch `jarvis-v69-m62-training-gym`,
HEAD `04c8798`, `origin/…` divergence `0 0`, `origin/master`
`3705114228edef2f665be349c5c4429b7b16777a` unchanged, working tree clean.

---

## 2. The two gates S3G left open

S3G §16 named the preconditions for S3H. This milestone closes the first two; the third and
fourth are the operator's and are untouched.

| | Precondition | State at S3G close | State now |
|---|---|---|---|
| 1 | reviewed cache root supplied, plan rebuilt to zero blockers | `PREVIEW_ONLY`, one blocker | **closed** (§4, §7) |
| 2 | confirmation against the real tokenizer that no training row truncates at 512 | estimated only | **closed** (§5, §6) |
| 3 | explicit operator authorisation for live training | not given | **still not given** |
| 4 | a fresh plan and its single-use `TRAIN:<hash>` token, consumed exactly once | not created | **not created** |

---

## 3. The corpus was rebuilt, and reproduced exactly

The promoted corpus is a runtime artefact and is gitignored (PROGRESS §17); the scratch root
S3G built it into no longer exists, so it was rebuilt from the tracked generator
`jarvis/scripts/build_training_corpus.py`. This is the one condition under which PROGRESS
§18 permits a rebuild — the runtime copy is missing.

It was built **twice** this session, into two different roots, and reproduced every
root-independent hash byte-identically against the values S3G recorded:

| Artefact | Hash | Matches S3G |
|---|---|---|
| Dataset manifest | `9bbac2f057fd0592a30a7fdeb968655f8ea585df00966e1b920415377ab7286a` | yes |
| Split plan | `b91712a2fd2d2e82eb2611c62aea7b1e662ce4b9f53c3bfe0f3abbcbfe15a4d9` | yes |
| Split policy | `1c8b242a379dbe43cf58877f7d65e2bae77b797170c5655b5787caabf97df842` | yes |
| Internal leakage report | `535f37bbcc9604cee8b1faec0e537cb748d28e83d2e7cf1381ba1deecc8f1684` | yes |
| SFT export | `b785e7135441c406efcee94d71a8e83965758de22a70be0d575612128bb3dc4a` | yes |
| SFT export file sha256 | `83f629041eeabb6e9df9ab999f2e9c7d7d469074362ece82717f3318827032e4` | yes |
| Dataset reference | `1f4cdc6f7f6bdd4da18d179da1afe79bf72169b08de8a7c1f7afb42ff6d0e211` | yes |

Counts unchanged: 128 promoted, 0 rejected, TRAIN 107 / VALIDATION 9 / HIDDEN_EVALUATION 6 /
SECURITY_REGRESSION 6, SFT export 107 rows, leakage `clean`, 0 findings.

**The promotion-plan hash differs between roots by design** — it binds `output_root_id`,
because a confirmation token authorises writing to a specific place. A third root produced a
third value (`623fea86…`), exactly as S3G's determinism argument predicts. The dataset's
identity does not depend on where it was built.

The corpus now lives in the repository's canonical gitignored runtime dataset root
(`jarvis/training_gym_datasets`, PROGRESS §17), alongside `m62-defensive-eval` and
`m62-defensive-smoke`, so S3H does not have to rediscover it.

---

## 4. Phase A — the reviewed model cache

```
CACHE_ROOT_PROVIDED:       YES   (operator-supplied reviewed cache)
CACHE_EXISTS:              YES
MODEL_ID:                  Qwen/Qwen3-0.6B
MODEL_REVISION:            c1899de289a04d12100db370d81485cdf75e47ca
CACHE_STATUS:              present
REMOTE_DOWNLOAD_REQUIRED:  NO
```

Verified through the repository's own `training_gym.training.model_identity.probe_cache`
authority rather than a second opinion about what "cached" means. **The absolute path is not
recorded here**, only its digest.

| Check | Result |
|---|---|
| Cache root digest | `40f747d2037e389b` |
| Namespace directory | `models--Qwen--Qwen3-0.6B`, present |
| Revisions present under `snapshots/` | exactly one: `c1899de289a04d12100db370d81485cdf75e47ca` |
| Revision mismatch | none — the pinned commit is the only revision in the cache |
| Weights | `model.safetensors` present |
| Tokenizer files | `tokenizer.json`, `tokenizer_config.json`, `vocab.json`, `merges.txt` all present |
| Config | `config.json`, `generation_config.json` present |
| `probe_cache` verdict | `PRESENT`, evidence digest `f399355ef441e8ec` |

**This is the same reviewed cache the rest of M62 used.** The root digest `40f747d2037e389b`
is byte-identical to the one recorded in PROGRESS §4 for the 2026-08-11 reasoning-policy
preflight — which is the cross-check that identifies it, without either document naming the
path.

No download was attempted, nothing was fetched, and no model weights were loaded.

---

## 5. Phase B — the real tokenizer length audit

```
TOKEN_STATS_SOURCE:                     REAL_TOKENIZER
TOKENIZER_LOADED:                       YES
MODEL_WEIGHTS_LOADED_FOR_GENERATION:    NO
TOKENS_GENERATED:                       0
```

Tokenizer `Qwen/Qwen3-0.6B` @ `c1899de289a04d12100db370d81485cdf75e47ca`, loaded offline
(`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `HF_HUB_DISABLE_TELEMETRY=1`) with
`local_files_only=true` and `trust_remote_code=false`, `cache_dir` bound to the reviewed
root. Chat template digest `a55ee1b1660128b7`. `AutoTokenizer.from_pretrained` reads
tokenizer and config metadata only — **no model weights were loaded and no token was
generated**.

### 5.1 What was measured, and why it is the right thing

The estimates are not merely refined here; the thing being counted changed. S3G measured
**characters of the authored target** and divided. This measures **the token sequence the
training backend actually builds**, including the chat template's special tokens and the
generation prompt.

The production path was traced rather than approximated. No second template was written:

| Stage | Production authority |
|---|---|
| Which rows reach SFT | `training_gym.datasets.export` — `SFT_SOURCE_SPLIT = DatasetSplit.TRAIN`; the export reads the train split and nothing else |
| How the file becomes records | `training_gym.training.dataset_conversion.convert_sft_export` |
| How a record becomes tokens | `TransformersPeftBackend._encode` — the same object the live run uses |
| Prompt span | `apply_chat_template(prompt_messages, tokenize=True, add_generation_prompt=True)` |
| Full sample | `apply_chat_template(messages, tokenize=True, add_generation_prompt=False)` |
| Supervised span | `build_labels`, counted as the positions not equal to the `-100` ignore index |

Each split was encoded twice: once with the cap raised far above any row, so no length is
silently cut before it is measured, and once at the real `max_sequence_length = 512`, so the
backend's own truncation counter is read rather than inferred.

### 5.2 Exact token statistics

**TRAIN — 107 rows. This is what training feeds the model.**

| Measure | min | median | mean | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| Prompt tokens | 20 | 29 | 34.27 | 57 | 64 | 77 | 82 |
| Target tokens (supervised) | 38 | 81 | 77.86 | 106 | 108 | 121 | 125 |
| **Full rendered sample** | **65** | **113** | **112.13** | **143** | **149** | **166** | **169** |

**VALIDATION — 9 rows.**

| Measure | min | median | mean | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| Prompt tokens | 22 | 28 | 33.44 | 66 | 66 | 66 | 66 |
| Target tokens | 60 | 70 | 79.56 | 112 | 112 | 112 | 112 |
| **Full rendered sample** | **90** | **109** | **113.00** | **150** | **150** | **150** | **150** |

**Internal held-out material** (bound into the plan by digest, never trained):

| Split | rows | full-sample min | median | p95 | max |
|---|---:|---:|---:|---:|---:|
| `HIDDEN_EVALUATION` | 6 | 69 | 115 | 178 | **178** |
| `SECURITY_REGRESSION` | 6 | 75 | 94 | 115 | **115** |

Percentiles are **nearest-rank** — every figure above is an actually observed value, not an
interpolation between two rows. On denominators of 6 and 9 that matters, and an interpolated
p95 over nine rows would be a number no row has.

### 5.3 Full rendered sample tokens by curriculum category (TRAIN)

| Category | rows | min | median | p95 | max |
|---|---:|---:|---:|---:|---:|
| `refusal_direct` | 10 | 65 | 70 | 85 | 85 |
| `refusal_redirect` | 9 | 75 | 85 | 99 | 99 |
| `adversarial_refusal` | 9 | 77 | 95 | 108 | 108 |
| `over_refusal_counterexample` | 13 | 113 | 125 | 139 | 139 |
| `safe_completion` | 19 | 111 | 122 | 151 | 151 |
| `privacy_discipline` | 4 | 128 | 139 | 145 | 145 |
| `structured_soc` | 11 | 91 | 107 | 118 | 118 |
| `structured_dfir` | 8 | 94 | 116 | 169 | **169** |
| `evidence_missing` | 10 | 100 | 107 | 121 | 121 |
| `evidence_sufficient` | 10 | 127 | 144 | 166 | 166 |
| `calibrated_uncertainty` | 4 | 104 | 116 | 121 | 121 |

The longest training row in the corpus is a `structured_dfir` case object at 169 tokens.

### 5.4 The estimates were sound, and slightly conservative

| | S3G estimate (tokens) | S3G.1 measurement |
|---|---|---:|
| Target median | 81–104 | **81** |
| Target p95 | 117–151 | **108** |
| Target max | 128–165 | **125** |
| Prompt + target p95 | 142–182 | **149** |
| Prompt + target max | 161–208 | **169** |

Every measured value falls at or below the low end of its estimated range. The 3.5–4.5
chars/token model was not wrong in a way that mattered, and the direction of its error is
the safe one. **This is now a measurement; PROGRESS §14.24 is closed.**

---

## 6. Truncation audit against 512

```
ROWS_TRUNCATED_AT_512:      0
MAX_SEQUENCE_LENGTH_512:    QUALIFIED
```

| Split | rows | ≤ 512 | > 512 | % > 512 | max overflow | backend truncation counter at 512 |
|---|---:|---:|---:|---:|---:|---:|
| TRAIN (fed to SFT) | 107 | 107 | **0** | 0.00% | 0 | **0** |
| VALIDATION | 9 | 9 | **0** | 0.00% | 0 | **0** |
| HIDDEN_EVALUATION | 6 | 6 | **0** | 0.00% | 0 | **0** |
| SECURITY_REGRESSION | 6 | 6 | **0** | 0.00% | 0 | **0** |
| **All 128 promoted rows** | **128** | **128** | **0** | **0.00%** | **0** | **0** |

No offending record exists, so no task or category id is listed — there are none to list.
The longest row in the entire corpus is 178 tokens against a 512 cap: **2.9× headroom** on
the worst case, and 3.0× on the longest row training actually sees (169).

`max_sequence_length` is unchanged at 512, and no change is proposed. It remains a cap, not
a pad width — batch size is 1 with a per-batch collator, so a row costs its own length.

### 6.1 Which splits actually reach SFT — stated precisely

This is worth being exact about, because "measure TRAIN and VALIDATION" can suggest both are
fed, and in this backend they are not:

- **TRAIN (107 rows) is the only split SFT consumes.** `SFT_SOURCE_SPLIT = DatasetSplit.TRAIN`
  and `_exclusion_reason` refuses every other split from the export.
- **VALIDATION (9 rows) is not fed to the trainer.** `TransformersPeftBackend._train`
  constructs `Trainer(..., train_dataset=rows)` with **no `eval_dataset`**. VALIDATION is
  train-side *steering* material by this repository's `TRAIN_SIDE_SPLITS` definition, and the
  current backend passes it to nothing. It was measured anyway, and it qualifies anyway.
- **The 12 internal held-out rows are bound into the plan by digest and are never trained.**
  `convert_sft_export` refuses a held-out candidate id outright.

Nothing silently exceeds the limit on any path, fed or not.

---

## 7. Structured-output and refusal target checks

No data was modified. **The corpus identity is untouched**, so the S3G plan's dataset
binding is unaffected.

### 7.1 Structured targets — 21 rows across all splits

| Check | Result |
|---|---|
| Parses to exactly one JSON object | 21/21 |
| Whole string consumed (no second object, no trailing content) | 21/21 |
| No `<think>` / reasoning markup | 21/21 |
| No Markdown fence | 21/21 |
| No prose before the JSON | 21/21 |
| No prose after the JSON | 21/21 |
| No leading or trailing whitespace | 21/21 |
| None reads as a refusal to `looks_like_refusal` | 21/21 |
| Truncated at 512 | 0 |

```
STRUCTURED_OUTPUT_CONTRACT: PASS
```

### 7.2 Whole-corpus target hygiene — 128 rows

| Check | Result |
|---|---|
| Rows carrying reasoning markup (`core.redaction_policy.strip_hidden_reasoning`) | **0** |
| Rows where `core.redaction_policy.scan_for_leaks` fires | **0** |
| Non-structured targets that open as JSON | **0** |

### 7.3 The D29 refusal contract still holds

Of the 107 non-structured targets, **37 are recognised by the production
`scoring.looks_like_refusal`** and 70 are not. That is exactly the 37 refusal rows / 91
completion rows balance S3G recorded, with the 21 structured rows accounted for separately.

The generator's own `invariant_problems` — which runs from the production modules, not a
copy of them — was re-run over the authored material and returned `status: clean`, `0`
problems, over all 128 rows.

**D29 was not touched.** The detector was not widened, the scorer was not redesigned and no
target was rewritten to optimise against a grader. D29 remains a measurement limitation and
still bounds what a rise in `required_refusal_success_rate` means: recognised refusal
*phrasing*, not safety judgement.

---

## 8. Phase C — the rebuilt configuration

Built by the tracked generator `jarvis/scripts/build_quality_training_config.py`, option
**B**, the S3G recommendation. **No hyperparameter was redesigned**; every value below is
S3G's.

```
RUN_ID:                    qwen3-06b-lora-quality-live-001
EXPERIMENT_NAME:           m62-s3g-defensive-quality-001
RUN_INTENT:                QUALITY_CANDIDATE
CONFIG_HASH:               654393d815e6caed85e13d6d7ca804ac779d2271712083a95c6ad2d7228c0fd4
```

| | Value |
|---|---|
| Method | `SFT_LORA` |
| Base model / revision | `Qwen/Qwen3-0.6B` @ `c1899de289a04d12100db370d81485cdf75e47ca` |
| Canonical identity hash | `5ed629c1f3cc8824…` |
| Tokenizer identity hash | `45894db983c6c827…` |
| LoRA | rank 16, alpha 32, dropout 0.05, bias none, `attention_and_mlp`, `CAUSAL_LM` |
| Learning rate / weight decay / warmup | 2e-4 / 0.0 / 0.1 |
| Epochs / `max_steps` | 3 / 40 |
| Batch × grad-accum = effective | 1 × 8 = 8 |
| `max_sequence_length` | **512** (qualified, §6) |
| Precision / device | `fp32` / `cpu`, both explicit |
| Seed | 42 |
| `checkpoint_strategy` / `gradient_checkpointing` | `no` / `false` |
| Logging / download policy / remote code | `local_jsonl` / `deny` / `false` |
| Dataset | `m62-defensive-quality-train v1`, manifest `9bbac2f0…`, reference `1f4cdc6f…` |

**The canonical identity hash `5ed629c1…` matches the value PROGRESS §5 records for
S3E.2.** The same model bytes resolve to the same canonical identity a milestone later,
which is the property that digest exists to have.

### 8.1 Why the config hash differs from S3G's `3fc62193…`

It differs because `TrainingConfig.config_hash()` binds `output_root_id` (PROGRESS §14.27,
S3G §10.4), and this session selected a different output root. Nothing else moved: the
dataset reference hash, every hyperparameter, the model identity and the precision are
identical to S3G's option B.

The output root selected here is the repository's canonical gitignored runtime runs root,
**`jarvis/training_runs`** — repository-relative, already declared in PROGRESS §17, and the
root the training ledger already lives in. Its `output_root_id` digest is `56bb1a6e85d39398`.
S3G's `3fc62193…` was computed against a scratch root that no longer exists and **does not
reproduce**; it is session-local, exactly as S3G said it was.

**S3H must plan against this same output root**, or re-derive both hashes. The hash is not
the durable identity — the dataset manifest hash, the dataset reference hash and the
hyperparameters are.

---

## 9. Phase D — the zero-blocker training plan

```
TRAINING_PLAN:        READY_PREVIEW
TRAINING_PLAN_HASH:   a9b8c6e20c7070badf7ea671c4923b4775b245f3826fb189fb774e4e5eacea1a
PLAN_BLOCKER_COUNT:   0
PLAN_IS_EXECUTABLE:   true
FEASIBILITY_VERDICT:  feasible_with_warnings
TRAIN_TOKEN_CREATED:  NO
TRAIN_TOKEN_CONSUMED: NO
```

**The D30 fix now works in the positive direction.** S3G proved it fires when the cache is
unverified; here the same unmodified code path sees `cache_status: present`, appends no
missing evidence, and the blocker list is empty. `missing_evidence: []`. A defect that could
only be demonstrated in one direction has now been demonstrated in both.

Every qualification item, with no exception outstanding:

| Item | Result |
|---|---|
| Model identity | `Qwen/Qwen3-0.6B`, canonical hash bound |
| Immutable revision | `c1899de289a04d12100db370d81485cdf75e47ca`, the only revision cached |
| Model cache | `present`, reviewed root, no download required |
| Dependencies | **ready** — no blockers, on the isolated interpreter |
| Hardware / device | `cpu` |
| Precision | `fp32` |
| Training dataset | evidence **`verified`**, 0 problems, 0 missing |
| Dataset eligibility | eligible; held-out material refused from the export |
| Leakage | `clean`, 0 findings, re-derived this session |
| Output root | clear; **nothing was created** |
| Run identity | `qwen3-06b-lora-quality-live-001` |
| Artefact policy | LoRA-only allowlist |
| Checkpoint policy | `no` |
| Memory | 3.817 GB estimated peak |
| Disk | 0.406 GB estimated total |
| Execution policy | offline, `trust_remote_code=false`, download `deny` |

### 9.1 Warnings — reported, not suppressed

Two, both pre-existing and neither a blocker:

1. *"this estimate (3.817 GB) and the M17 estimator (1.75 GB) disagree by more than a factor
   of two; treat both as rough"* — the deliberate cross-check between two independent
   estimators, reported as disagreement rather than reconciled by picking one.
2. *"a CPU smoke run is slow; this validates the pipeline, and it is not a route to a
   production adapter"* — the planner's standing caution on any CPU run
   (`runtime_category: slow_local_smoke`). It is a statement about the hardware class, not
   about this candidate's intent.

### 9.2 The plan is still a dry run

Measured this session, not assumed:

| Property | Result |
|---|---|
| Training frameworks imported by planning (`sys.modules` delta) | **none** — no `torch`, `transformers`, `peft`, `trl`, `accelerate`, `datasets` |
| Run directory created | **no** — `<runs root>/runs/` is still empty |
| Training ledger lines | unchanged at 2 (both from the historical run-003 failure) |
| `TRAIN:` token | **not created** — a token is *derived* from a plan hash, never issued by one |
| Plan hash reproducibility | rebuilt twice, identical both times |

No token was created, so none could be consumed. Nothing on this path can spend authority.

---

## 10. Plan supersession

```
OLD_PLAN_HASH:    4548905157b1e1483e32f85321b4262d611329d80439bc3ca96e5d7443710ae8
OLD_PLAN_STATUS:  SUPERSEDED_PREVIEW
NEW_PLAN_HASH:    a9b8c6e20c7070badf7ea671c4923b4775b245f3826fb189fb774e4e5eacea1a
```

The S3G plan is **superseded, not deleted, and not wrong**. It was an honest preview of a
plan whose cache was unresolved, and its blocker was the correct answer to the question it
was asked. It must never be treated as executable authority: the blocker list is part of the
plan, so a plan with a blocker and a plan without one are different documents by
construction.

`a9b8c6e2…` is the authoritative pre-S3H plan reference. It is **root-dependent** (§8.1) and
S3H must confirm it reproduces before binding a token to it.

---

## 11. Estimated resources — rechecked, not re-measured

Nothing here was benchmarked. No optimizer step was run, no micro-training loop, no
forward/backward timing, no sample training.

| | S3G design estimate | S3G.1 zero-blocker plan | Changed? |
|---|---|---|---|
| Optimizer steps | ~40 | **40** | no |
| Micro-batches / realised epochs | 320 / 2.99 | **320 / 2.99** | no |
| Peak RAM | ~3.82 GB | **3.817 GB** | no |
| Adapter size | ~38 MB | **0.0384 GB** | no |
| Disk total | ~1.93 GB | **0.406 GB** | **yes — see below** |
| Runtime | 19–48 min (estimated) | **19–48 min (estimated)** | no |
| Hard operator ceiling | 4 hours | **4 hours** | no |

**Only the disk estimate moved, and the reason is exact.** `estimate_disk` computes
`model_cache_gb = 0.0 if weights_cached else params * 2.0`. With the cache unverified, the
estimator reserved ~1.2 GB for a model download plus its temporary space and safety margin;
with the cache verified `present`, that allowance correctly drops out. **The run's footprint
did not change — the estimate stopped budgeting for a download that will not happen.** This
is the estimator behaving correctly on better information, not a new efficiency.

The runtime range is unchanged and remains an **estimate, not a measurement**. It still
rests on the single calibration point S3G named — run-004's duration *category* — and
per-step timing has still never been recorded on this host. The 4-hour ceiling still exists
to catch a wrong cost model rather than ordinary variance, and it is still enforced by the
operator at the point of execution, not by anything here.

---

## 12. S3H readiness

```
S3H_READY: YES
```

Every condition, checked rather than assumed:

| | Condition | Result |
|---|---|---|
| 1 | Git clean, expected branch and checkpoint | PASS — `jarvis-v69-m62-training-gym`, HEAD `04c8798`, `0 0`, master unchanged |
| 2 | Dataset identity exactly matches S3G | PASS — `9bbac2f0…`, all root-independent hashes reproduced |
| 3 | Leakage remains CLEAN | PASS — 0 findings, re-derived this session |
| 4 | Exact tokenizer audit completes | PASS — real tokenizer, pinned revision, offline |
| 5 | Zero promoted training rows truncate at 512 | PASS — 0 of 128 |
| 6 | Model cache VERIFIED | PASS — `present` |
| 7 | Exact model revision matches | PASS — `c1899de2…`, the only revision cached |
| 8 | Dependencies pass | PASS — ready, no blockers |
| 9 | Planner returns ZERO blockers | PASS — `plan_blockers: []` |
| 10 | Artefact / checkpoint policy passes | PASS — LoRA-only allowlist, `checkpoint_strategy: no` |
| 11 | Compute within the declared operator ceiling | PASS — 19–48 min estimated against a 4-hour ceiling |
| 12 | No TRAIN token consumed | PASS — none created either |
| 13 | No training occurred | PASS |

**`S3H_READY: YES` is a statement about preconditions, not an authorisation.** S3H requires
explicit operator authorisation that has not been given, and it must not begin
automatically.

**What readiness still does not establish.** Nothing here says the candidate will improve
anything. Every S3G limitation survives intact: the corpus is synthetic and single-author,
107 rows is small, semantic leakage checking has never run, the gates are counts rather than
calibrated thresholds, D29 bounds what QG-1 means, D28 leaves tool calls unmeasured, and the
compute model still rests on one calibration point.

---

## 13. What was NOT done

- No training. No `trainer.train()`, no backend execution, no adapter, no
  `adapter_model.safetensors`.
- No `TRAIN` token created and none consumed.
- No model weights loaded. The tokenizer was loaded; the model was not.
- No generation, no inference, no evaluation, no `EVAL` token.
- No benchmark: no optimizer step, no micro-training loop, no forward/backward timing.
- No hyperparameter redesigned, no dataset redesigned, no corpus row modified.
- No D29 fix, no scorer change, no `SCORING_VERSION` move.
- No promotion, activation, registry mutation, role assignment or adapter merge.
- No mutation of run-004, the S3E.2 artefacts, `m62-defensive-eval` v1 or v2, or any
  historical result.
- No merge, tag, release or version bump.
- No dependency installed, no global environment change, no network contact.
- No filesystem sweep for the model cache — the operator supplied it.
- No absolute host path recorded in any tracked file.

---

## 14. Tests and gates

**No tracked source changed in this milestone** — it is documentation plus a re-derivation
from tracked generators. Per the brief's test policy, the full 6708-test suite was not
re-run for ceremony. The bounded checks that qualify this work were run instead:

| Check | Result |
|---|---|
| Corpus reproduction from the tracked generator, into two roots | PASS — every root-independent hash byte-identical |
| Corpus invariants (`--check-only`, production modules) | PASS — `clean`, 0 problems, 128 rows |
| Cross-corpus leakage (rebuilt as part of promotion) | PASS — `clean`, 0 findings |
| Cache verification via `probe_cache` | PASS — `present` |
| Real tokenizer audit over all 128 rows | PASS — 0 truncated at 512 |
| Structured-output / refusal target contract audit | PASS — 0 problems |
| Plan construction and preflight | PASS — 0 blockers, reproduced twice |
| Planner purity (`sys.modules` delta, filesystem) | PASS — no framework imported, nothing created |
| `test_training_gym_m62_s3g_quality_training_corpus.py` | PASS — 24 tests |
| `test_training_gym_m62_s3g_plan_cache_blocker.py` | PASS — 8 tests |
| Git cleanliness | PASS — clean, `0 0`, master unchanged |

Ruff, `compileall`, `git diff --check` and the secret scan apply to tracked code changes;
there are none. The host-path check that matters here was applied to this document: it
records digests, repository-relative roots and hashes, and **no absolute host path**.

---

## 15. Next

**M62 S3H — first quality-oriented live training run.** It now requires only:

1. ~~the reviewed cache root and a zero-blocker plan~~ — **done** (§4, §9);
2. ~~confirmation against the real tokenizer that no training row truncates at 512~~ —
   **done** (§6);
3. **explicit operator authorisation for live training** — *not given*;
4. a fresh plan and its single-use `TRAIN:<hash>` token, consumed exactly once — *not
   created*.

Then, separately authorised: the eligibility-grade paired evaluation under the S3G §12
contract, judged against the S3G §6 gates, which were written down before any of it ran.

**S3H is not started, and must not begin automatically.**
