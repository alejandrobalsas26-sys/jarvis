# V69 M62 S3G.2 — Train-side validation wiring, eval-loss observability, and final S3H readiness

**Date (UTC):** 2026-08-13
**Status:** WIRING COMPLETE — **TRAINING NOT RUN**
**Scope:** trace why the promoted VALIDATION split reached the trainer as nothing, implement the
smallest correct production wiring, make train and validation loss observable, qualify the
result offline, rebuild the plan, and restate S3H readiness. Nothing was trained, no model
weights were loaded, no token was created and none was consumed.

```
S3G2_VALIDATION_WIRING:  PASS
TRAINING_EXECUTED:       NO
ADAPTER_CREATED:         NO
TRAIN_TOKEN_CREATED:     NO
TRAIN_TOKEN_CONSUMED:    NO
MODEL_REGISTRY_MUTATED:  NO
```

This document does **not** revise S3G or S3G.1. S3G designed the candidate; S3G.1 qualified it
for execution and, in §6.1, recorded precisely the fact this milestone acts on — *"VALIDATION
(9 rows) is not fed to the trainer"*. That sentence was accurate then and remains the honest
record of it. What changed here is the **production code**, not the finding.

---

## 1. Authorisation and boundary

The operator authorised **M62 S3G.2 — train-side validation wiring qualification**: inspect the
production training path, determine and implement the smallest correct wiring for the existing
VALIDATION split, expose validation loss during future training, preserve every M62
artefact-security rule, qualify the resulting configuration and plan offline, add focused
regression tests, update documentation and PROGRESS.md, commit and push.

It authorises **none** of: live training, `trainer.train()`, creating or consuming a `TRAIN`
token, adapter creation, model generation, live inference, live evaluation, consuming an `EVAL`
token, early stopping, promotion, activation, Model Registry mutation, merge, tag, release or
version bump. Every one of those remains not done.

**Starting checkpoint (verified, not assumed):** branch `jarvis-v69-m62-training-gym`, HEAD
`290f7d75374b97b88ae86e6839a3ea8cff86d04a`, `origin/…` divergence `0 0`, `origin/master`
`3705114228edef2f665be349c5c4429b7b16777a` unchanged, working tree clean.

---

## 2. The finding S3G.1 handed over

S3G.1 §6.1 stated it exactly, and it is worth quoting rather than paraphrasing:

> **VALIDATION (9 rows) is not fed to the trainer.** `TransformersPeftBackend._train`
> constructs `Trainer(..., train_dataset=rows)` with **no `eval_dataset`**. VALIDATION is
> train-side *steering* material by this repository's `TRAIN_SIDE_SPLITS` definition, and the
> current backend passes it to nothing. It was measured anyway, and it qualifies anyway.

Nine rows had been through the entire authority chain — approved, deterministically assigned by
`plan_splits`, promoted, bound into the training plan by two digests
(`validation_split_manifest_hash` and `validation_shard_hash`), and length-audited against the
real pinned tokenizer — and then read by nothing.

**Why it matters, stated without inflation.** It does not make the adapter worse, and it does
not invalidate any S3G or S3G.1 conclusion. What it costs is *diagnosis*. The first
quality-oriented run takes 40 optimizer steps at rank 16 over 107 rows, and S3G §10.3 named the
failure mode it is most exposed to: *"a good validation loss and a worse held-out result — the
one that is hardest to detect and easiest to believe."* Detecting that needs a validation curve
next to the training curve. Without one, the run reports a single training loss, and the first
evidence of overfitting would arrive from the eligibility evaluation — after the token is spent,
under a paired comparison that costs hours of CPU, and confounded with every other variable in
it. S3G's own compute table already claimed the protection it did not have: *"moderate; watched
by VALIDATION."*

A second, smaller cost: `BackendResult.eval_loss` and `AdapterManifest.eval_loss` both existed
and both reported `0.0` — the dataclass default. A field that always reads zero is worse than an
absent one, because it looks like a measurement.

---

## 3. Root cause — three boundaries, not one

Traced through the production path, in order, rather than reconstructed from memory. Every
claim below is a line of code at the starting HEAD.

| # | Stage | Production authority | What it did |
|---|---|---|---|
| 1 | Which rows may be exported | `training_gym/datasets/export.py` | `SFT_SOURCE_SPLIT = DatasetSplit.TRAIN`; `SFTExportManifest` refused any `source_split` but `train` and any `filename` but `sft_train.jsonl`. **No artefact containing the validation rows existed in a shape a trainer could read.** |
| 2 | What the run is a function of | `training_gym/training/backend.py` | `ExecutionRequest.validation_file: Path \| None` — the field had existed since S3B. |
| 3 | Who fills it | `training_gym/training/execution.py` | `validation_file=None`, **hard-coded, at two sites** (`_request`, and the request built inside `_run`). |
| 4 | The trainer | `transformers_peft.py` `_train` | `Trainer(model=…, args=…, train_dataset=rows, data_collator=…)` — **no `eval_dataset`**. |
| 5 | The reported number | same, and `artifacts.py` | `eval_loss=float(metrics.get("eval_loss", 0.0))` read from `train().metrics`, which carries training metrics only. Always `0.0`. |

**The interesting part is (2) against (3).** This was not a missing feature: the seam was
designed, the field was typed `Path | None`, and the caller wrote `None` into it
unconditionally. `plan_training` was already binding both validation digests into the plan
hash. Everything was in place except the one line that would have carried the rows across.

Recorded as **D31**.

---

## 4. What was implemented, and what was deliberately not

**Blast radius: 10 tracked files, one new test file.** No parallel training architecture, no
refactor of an unrelated M62 component, no new dataset, no changed row.

### 4.1 A sibling export authority — additive, not a widening

`export_sft` and `SFTExportManifest` carry a security invariant with a stated reason: *"an SFT
export reads the train split and nothing else, because every other split exists to measure the
model rather than to fit it."* That invariant was **not relaxed**. Instead:

* `export_sft_validation` and `verify_sft_validation_export` are new public entry points, each
  hard-bound to one split and one pair of filenames (`sft_validation.jsonl` /
  `sft_validation.manifest.json`);
* both they and the train-side pair delegate to one private `_export_split`, so there is a
  single implementation of row selection, the five redundant filters, the per-row hidden-key and
  redaction scans, the immutability check and the write;
* `EXPORTABLE_SPLITS` is a **closed table** mapping split → filenames. It is the whole
  authorisation. `HIDDEN_EVALUATION`, `SECURITY_REGRESSION`, `ADVERSARIAL` and `QUARANTINE` are
  absent from it, and there is no argument, config field or CLI flag that adds them;
* the manifest now checks the **pair** `(source_split, filename)` against that table rather than
  the two fields independently — which is *stricter* than before, because a manifest naming one
  split under the other's filename was previously expressible.

**The train export's identity does not move.** `_exclusion_reason` builds its reason string from
the split it was asked for, so `not_train_split` keeps exactly the spelling it had. That matters
because `excluded_counts` feeds `export_hash`: a renamed reason would re-hash every train export
ever written. Verified against the promoted corpus — `b785e713…`, unchanged, `excluded_counts`
still `{}`.

### 4.2 A cadence in the config, bound to identity

`ValidationStrategy` — `no` / `epoch` / `steps` — on `TrainingConfig.validation_strategy`,
defaulting to `no`. `steps` is *named* so the refusal can say what was asked for, and refused:
nine validation rows against a forty-step run means re-measuring the same nine rows forty times,
charging the cost to every step, and producing a curve whose movement is sampling noise. This
mirrors `CheckpointStrategy` exactly, including why the unsupported members stay in the
vocabulary.

`SUPPORTED_VALIDATION_STRATEGIES = {NO, EPOCH}`.

### 4.3 The wiring itself

`validation_file` threaded through `run_preflight` → `execute_training` → `_run` →
`ExecutionRequest`, and consumed by the backend: `convert_sft_export` on the validation export,
the **same** `_encode`, the **same** `build_labels`, the **same** `_masking_self_test`, then
`Trainer(..., eval_dataset=eval_rows)`.

The CLI derives the path from the config, never from a flag. There is no way to hand a
validation corpus to a config that did not ask for one, and no way to suppress it for a config
that did — **both** directions are refused by `readiness()`, before the plan is spent.

### 4.4 What was deliberately not done

* **No early stopping.** No `EarlyStoppingCallback`, and any callback whose type name contains
  `EarlyStopping` is stripped from the handler after construction. Nine rows do not decide when
  a run ends.
* **No `load_best_model_at_end`.** Passed explicitly as `False` rather than left to a default,
  because `True` is precisely what makes `Trainer` demand that `save_strategy` match
  `eval_strategy` — the route back to the pickle-shaped checkpoint state D16 closed.
* **No generation.** No `generate`, no `predict`, no `compute_metrics`, no `GenerationConfig`.
  Evaluation is teacher-forced cross-entropy over targets the corpus already contains.
* **No new dataset, no changed row, no D29 fix, no `SCORING_VERSION` move.**
* **No hyperparameter redesigned.** Option B is S3G's, value for value.

---

## 5. Split semantics — stated precisely

```
TRAIN                107 rows   →  Trainer(train_dataset=...)   fitted on, contributes gradients
VALIDATION             9 rows   →  Trainer(eval_dataset=...)    measured only, no gradient
HIDDEN_EVALUATION      6 rows   →  bound by digest, exported by nothing
SECURITY_REGRESSION    6 rows   →  bound by digest, exported by nothing
```

**VALIDATION is not held-out eligibility evidence, and this milestone does not make it any.**
It is train-side steering material by this repository's `TRAIN_SIDE_SPLITS` definition. Its loss
answers *"is this run fitting or memorising"*. It answers nothing about eligibility, it appears
in no S3G §6 acceptance gate, and it authorises no promotion.

The held-out eligibility corpus remains **`m62-defensive-eval v2`** (`10ad2308…`), post-training
and separately authorised. It is `evaluation_only`, `dataset_eligible: false`, and three
independent authorities refuse it a train-side split. A test asserts the string
`m62-defensive-eval` appears in the *executable source* of none of the four modules on the
train-time validation path.

**Measured isolation, over the promoted corpus:**

| Check | Result |
|---|---|
| TRAIN ∩ VALIDATION (candidate ids) | **∅** |
| VALIDATION export ids == VALIDATION shard ids | **yes** (9 / 9) |
| Internal held-out ids (12) in the train export | **0** |
| Internal held-out ids (12) in the validation export | **0** |
| `convert_sft_export` refuses a held-out id in *either* corpus | yes, by id |
| Rows in both `train_dataset` and `eval_dataset` objects | **∅** |

---

## 6. Evaluation cadence, and why one closing pass was added

```
EARLY_STOPPING:                 DISABLED
VALIDATION_EVALUATION_CADENCE:  once per epoch (eval_strategy="epoch") + one closing evaluation
```

Once per epoch, per the brief's strong default: three epochs, interpretable, and negligible
against a nine-row split.

**One detail the epoch cadence alone does not cover, and it is not cosmetic.** `max_steps=40`
bounds the run, not the declared epoch count — 40 × 8 ÷ 107 = **2.99 realised epochs**. The run
therefore stops *just before* the third epoch boundary, so an epoch-strategy evaluation fires at
epochs 1 and 2 and there may be no third boundary to fire at. The last periodic measurement
would then be the state of the weights at the end of epoch 2, not the weights the run actually
produces and saves.

So `_train` takes **one explicit `trainer.evaluate()` after `train()` returns**, and records it
separately as `final_evaluation` with `at_end_of_training: true`. The periodic curve and the
end-of-run number stay two distinguishable things; neither is presented as the other. One extra
forward-only pass over nine rows, no generation, no gradient, no checkpoint.

---

## 7. Checkpoint and artefact safety — nothing regressed

```
CHECKPOINT_SAVING:        DISABLED   (save_strategy="no", unchanged)
LOAD_BEST_MODEL_AT_END:   FALSE      (passed explicitly)
```

`eval_strategy` and `save_strategy` are coupled by exactly one thing —
`load_best_model_at_end` — which is never set. So evaluation runs while the trainer writes no
checkpoint at all. Verified in the objects the backend actually constructs, not asserted from
the source.

Three independent guards, in ascending order of how early they fire:

1. **Config.** `checkpoint_strategy` remains restricted to `no`, and a second explicit check
   refuses any non-`no` checkpoint strategy alongan enabled validation strategy. Unreachable
   today — which is the point: the refusal exists *before* the combination can, rather than
   after somebody widens the other enum.
2. **Arguments.** `save_strategy=config.checkpoint_strategy.value`, `save_total_limit`, and no
   `metric_for_best_model` / `greater_is_better` / `early_stopping_patience` anywhere.
3. **Artefacts.** The adapter allowlist and `FORBIDDEN_SUFFIXES` are untouched. A test asserts
   the run directory gains no `checkpoint*` entry and no `.bin` / `.pt` / `.pth` / `.pkl` /
   `.pickle` file.

`safetensors`-only final adapter: unchanged. `ADAPTER_MANIFEST_VERSION`: unchanged, deliberately
— run-004's manifest `06b1d3a3…` still verifies, because nothing was added to the manifest's
closed field list.

---

## 8. Validation encoding — same tokenizer, same template, same cap

The eval rows go through the identical production path as the training rows. This is asserted
structurally (`self._encode` is called exactly twice, with the same `tokenizer` variable and the
same `max_length=config.max_sequence_length`) and behaviourally (the encoded rows equal an
independent rendering of the same records).

| Property | Value |
|---|---|
| Tokenizer | `Qwen/Qwen3-0.6B` @ `c1899de289a04d12100db370d81485cdf75e47ca` |
| Loading | offline, `local_files_only=true`, `trust_remote_code=false`, reviewed `cache_dir` |
| Chat template | the model's own; rendering semantics **unchanged** by this milestone |
| `max_sequence_length` | **512**, unchanged |
| Label masking | `build_labels`, assistant-only, proven per row |

**`_masking_self_test` now runs over the eval arm too, and a failure refuses the run.** An eval
loss computed over prompt tokens is not the quantity the training loss is being compared
against, so the eval arm has to satisfy the same masking proof the training arm does.

**A disjointness backstop is checked at encode time**: if any candidate id appears in both
corpora the run is refused. `plan_splits` already makes them disjoint and each export reads one
shard, so it cannot fire today — it is written because the failure it names, measuring on rows
that were fitted, reports memorisation as generalisation, and it is the one defect a loss curve
cannot show.

### 8.1 The 512 qualification is not stale

**The wiring does not change the model-facing training representation.** Same tokenizer, same
immutable revision, same chat template, same `max_sequence_length`, same masking rules, same
`_encode`. So the S3G.1 audit of all 128 rows stands, and re-running it would re-measure a number
nothing moved. Per the brief, a **bounded validation-only** check was run instead.

```
VALIDATION_ROWS_TRUNCATED:  0 / 9
MAX_SEQUENCE_LENGTH:        512 QUALIFIED (unchanged)
```

Measured this session with the **real pinned tokenizer**, loaded offline from the reviewed
cache (root digest `40f747d2037e389b`, `probe_cache` → `present`, evidence digest
`f399355ef441e8ec` — all three byte-identical to S3G.1's, which is what identifies it as the
same reviewed cache without either document naming the path). Chat template digest
**`a55ee1b1660128b7`** — also byte-identical to S3G.1's, which is the direct evidence that the
rendering semantics did not move.

Encoded twice per split, as S3G.1 did it: once with the cap raised far above any row so nothing
is cut before it is measured, and once at the real 512 so the backend's own truncation counter is
read rather than inferred.

| Split | rows | min | max | uncapped max | > 512 | truncation counter at 512 | masking verified |
|---|---:|---:|---:|---:|---:|---:|---|
| **VALIDATION** (the new eval arm) | 9 | 90 | **150** | 150 | **0** | **0** | **yes** |
| TRAIN (control, unchanged) | 107 | 65 | **169** | 169 | **0** | **0** | **yes** |

Both reproduce S3G.1's figures exactly — validation max 150, train max 169. The eval arm's
longest row is 150 tokens against a 512 cap: **3.4× headroom**. `masking_verified` is `true` for
both arms through the production `_masking_self_test`, so the validation loss will be an
assistant-only loss, comparable to the training loss it is read against.

No model weights were loaded; `AutoTokenizer.from_pretrained` reads tokenizer and config metadata
only. **0 tokens were generated.**

---

## 9. Metric observability

`BackendResult.evidence["train_time_validation"]`, persisted in `backend_result.json` — an
already-allowlisted run-directory file, so no artefact policy changed and no manifest version
moved.

| Field | What it carries |
|---|---|
| `enabled`, `strategy`, `split` | the cadence actually applied |
| `validation_rows` | the sample count |
| `validation_rows_truncated` | the eval arm's own truncation count, separate from the training arm's |
| `eval_loss_by_evaluation` | per evaluation: `eval_loss`, `epoch`, `step`, and `eval_runtime` when transformers reports it |
| `final_evaluation` | the closing pass, flagged `at_end_of_training` |
| `final_eval_loss` | the end-of-run number, or the last periodic one when there is no closing pass — never one presented as the other |
| `train_loss_by_logging_step` | the training curve, so the two sit side by side without re-deriving one from another artefact |
| `assistant_only_loss_evidence` | the eval arm's masking proof |
| `early_stopping`, `load_best_model_at_end`, `contributes_gradients`, `generation_performed`, `is_held_out_eligibility_evidence` | all `false`, recorded so a number cannot travel without its caveats |

`BackendResult.eval_loss` and `AdapterManifest.eval_loss` now carry the closing measurement
instead of `0.0`.

**Numbers only, by construction.** Every leaf is an int, float, bool, or a string from a closed
vocabulary — there is no response body to carry, because a teacher-forced loss produces no
generation. A test walks every leaf and pins that. `_finite()` maps `NaN`/`±inf`/unparseable to
`0.0` before anything enters the curve: `BackendResult` already refuses a non-finite metric, but
the per-evaluation history had no such guard and would have persisted `NaN` as though it were
data.

A future S3H report can therefore print train loss, validation loss per epoch, the final
validation loss, epoch, global step, evaluation runtime and the validation sample count.

**No quality score is invented from validation loss.** It is diagnostic. It does not replace
`m62-defensive-eval v2` and it authorises no promotion.

---

## 10. The dataset did not change

```
TRAINING_DATASET:       m62-defensive-quality-train v1
TRAINING_DATASET_HASH:  9bbac2f057fd0592a30a7fdeb968655f8ea585df00966e1b920415377ab7286a
DATASET_MUTATED:        NO
```

Re-derived this session from the promoted artefacts on disk:

| Artefact | Hash | Moved? |
|---|---|---|
| Dataset manifest | `9bbac2f057fd0592a30a7fdeb968655f8ea585df00966e1b920415377ab7286a` | no |
| SFT **training** export | `b785e7135441c406efcee94d71a8e83965758de22a70be0d575612128bb3dc4a` | no |
| Training dataset reference | `1f4cdc6f7f6bdd4da18d179da1afe79bf72169b08de8a7c1f7afb42ff6d0e211` | no |
| SFT **validation** export | `589e056baff10690a58fca37b34d78612ea0c7ed0387a7a294fc27f05d978606` | **new** |
| Validation export file sha256 | `7a1429ddfb0d31e3a38b1936e8e95ffb3d7496e5f2566a5151da6cdfb3630b79` | **new** |

Counts unchanged: 128 promoted, TRAIN 107 / VALIDATION 9 / HIDDEN_EVALUATION 6 /
SECURITY_REGRESSION 6. Validation export: **9 rows, 0 excluded**, verifies.

**No row was modified.** The validation export is *derived* from the promoted VALIDATION shard,
after promotion, by the same filters the training export uses. The dataset reference deliberately
still binds the **training** export — binding the validation export there would move
`reference_hash`, and with it the identity the S3G.1 plan was built against, for a file the
reference never described. What binds the validation **rows** is
`validation_split_manifest_hash` / `validation_shard_hash`, which were already there and are
already inside the plan hash. A test pins both halves of that.

---

## 11. Configuration and plan identity

Because the run's behaviour changed, its identity had to. Because most configurations' behaviour
did *not* change, theirs had to not.

```
CONFIG_HASH:  b5f63cd8f65c7bc91c52b58b1d53a18bc757ff361d59f83b98e33f7a1dcafb03
```

`validation_strategy` appears in `TrainingConfig.to_dict()` — and therefore in `config_hash`,
and therefore in every plan hash derived from it — **only when it is not `no`**. Measured, both
directions:

| Configuration | `config_hash` |
|---|---|
| Option B, `validation_strategy=epoch` | `b5f63cd8f65c7bc91c52b58b1d53a18bc757ff361d59f83b98e33f7a1dcafb03` |
| The same option B, `validation_strategy=no` | `654393d815e6caed85e13d6d7ca804ac779d2271712083a95c6ad2d7228c0fd4` |

The second value is **byte-identical to the S3G.1 config hash**. That is the property the
value-gating exists for, and it is the half a one-sided test would miss: a fix that moved the
hash unconditionally would re-identify every configuration ever written, including the one the
S3G.1 plan was built from, while a fix that froze it would let two materially different runs —
one that measures its validation split, one that does not — share a single identity and a single
spendable token. Neither is acceptable; the canonical form gains the key at exactly the moment
the run gains the behaviour.

The same rule already governs `export.sft_row`, which emits a system turn only for a candidate
that has one, for the same reason: an always-present key whose value means "absent" makes two
different things look like one.

`TRAINING_SCHEMA_VERSION` stays `m62.training_config.1`. A document that omits the field means
`no` and round-trips to the same hash; a document that names it under a build too old to know it
is refused by `reject_unknown_fields` rather than silently losing the setting. Both pinned.

`plan.hyperparameters` gains `validation_strategy` and `validation_split` under the same
value-gate, so an operator reading `plan.to_record()` sees the cadence they are authorising
without re-deriving it from a config document.

### 11.1 The rebuilt plan

```
TRAINING_PLAN:        READY_PREVIEW
TRAINING_PLAN_HASH:   122efc62491256b25756eb24be37d3695347763295682f7409ea231293507ffe
PLAN_BLOCKER_COUNT:   0
PLAN_IS_EXECUTABLE:   true
FEASIBILITY_VERDICT:  feasible_with_warnings
TRAIN_TOKEN_CREATED:  NO
TRAIN_TOKEN_CONSUMED: NO
```

| Item | Result |
|---|---|
| Model identity / immutable revision | `Qwen/Qwen3-0.6B` @ `c1899de289a04d12100db370d81485cdf75e47ca` |
| Model cache | **`present`**, reviewed root, no download required |
| Dependencies | **ready**, no blockers, on the isolated interpreter |
| Training dataset | evidence **`verified`**, 0 problems, 0 missing |
| Leakage | unchanged; no row was touched |
| Device / precision | `cpu` / `fp32`, both explicit |
| Validation | `validation_strategy: epoch`, `validation_split: validation`, both in `plan.hyperparameters` |
| `validation_split_hash` | `99a19555d2059b0912360f7d06efe8b5ebb190ad3f2d35ba532d0276bfba9235` — unchanged, and it was already inside the plan hash |
| Checkpoint policy | `no` |
| Artefact policy | LoRA-only allowlist |
| Memory / disk | 3.817 GB peak / 0.406 GB total, both unchanged |
| Output root | clear; **nothing was created** |

**Warnings — reported, not suppressed.** The same two S3G.1 reported, both pre-existing, neither
a blocker and neither related to validation:

1. *"this estimate (3.817 GB) and the M17 estimator (1.75 GB) disagree by more than a factor of
   two; treat both as rough"* — the deliberate cross-check between two independent estimators.
2. *"a CPU smoke run is slow; this validates the pipeline, and it is not a route to a production
   adapter"* — the planner's standing caution on any CPU run. A statement about the hardware
   class, not about this candidate's intent.

**Still a dry run, measured rather than assumed:**

| Property | Result |
|---|---|
| Training frameworks imported by planning (`sys.modules` delta) | **none** — no `torch`, `transformers`, `peft`, `trl`, `accelerate`, `datasets` |
| Run directory created | **no** — `<runs root>/runs/` still empty |
| Training ledger lines | **unchanged at 2** (both from the historical run-003 failure) |
| Plan hash reproducibility | rebuilt twice in one process, identical both times |
| `TRAIN:` token | **not created** — a token is *derived* from a plan hash, never issued by one |

### 11.2 Plan supersession

```
OLD_PLAN_HASH:    a9b8c6e20c7070badf7ea671c4923b4775b245f3826fb189fb774e4e5eacea1a
OLD_PLAN_STATUS:  SUPERSEDED_PREVALIDATION_PREVIEW
NEW_PLAN_HASH:    122efc62491256b25756eb24be37d3695347763295682f7409ea231293507ffe
```

The S3G.1 plan is **superseded, not deleted, and not wrong.** It was an honest, zero-blocker
preview of a run that would not have measured its validation split — because at the time nothing
could. It must not be treated as executable authority for the current candidate: the two plans
authorise materially different runs, and that difference is exactly what the moved hash records.

The S3G plan `4548905157…` remains `SUPERSEDED_PREVIEW` from S3G.1. Neither is deleted.

**Both hashes are root-dependent** (PROGRESS §14.27). They were computed against the canonical
gitignored runs root `jarvis/training_runs` (`output_root_id` digest `56bb1a6e85d39398`). S3H
must plan against that same root or re-derive both. The root-independent identities are the
dataset manifest hash and the dataset reference hash `1f4cdc6f…`.

---

## 12. Estimated validation overhead

**Not measured. No optimizer step, no evaluation pass and no forward/backward timing was run.**

Derived from the same cost model S3G §9.2 declared, with its one calibration point unchanged:

* a LoRA forward+backward is ≈ `4 × parameters × tokens`; a forward-only evaluation pass is
  ≈ `2 × parameters × tokens`;
* training work: 320 micro-batches × ≈112 median tokens (S3G.1 measured) → ∝ `4 × 320 × 112` =
  143,360 parameter-token units;
* evaluation work: at most 4 passes (up to 3 epoch boundaries + 1 closing) × 9 rows × ≈113 median
  tokens → ∝ `2 × 4 × 9 × 113` = 8,136 units;
* ratio ≈ **5.7%** of the training compute;
* plus per-evaluation fixed cost on CPU — dataloader construction, metric aggregation — which is
  seconds, not minutes, but is not zero.

```
ESTIMATED_VALIDATION_OVERHEAD:  +1 to +4 minutes  (estimated, not measured)
ESTIMATED_TRAINING_RUNTIME:     20-52 minutes     (was 19-48; estimated, not measured)
HARD_RUNTIME_CEILING:           4 hours           (unchanged)
```

The ceiling is unchanged and unchanged on purpose: it exists to catch a wrong cost model, not
ordinary variance, and a 6% addition does not bear on that. It is still enforced by the operator
at the point of execution, not by anything here.

---

## 13. Tests

`tests/test_training_gym_m62_s3g2_validation_wiring.py` — **70 tests**, covering every item the
brief enumerated:

| Requirement | Covered by |
|---|---|
| 1 TRAIN rows → `train_dataset` | `test_the_train_rows_reach_train_dataset` |
| 2 VALIDATION rows → `eval_dataset` | `test_the_validation_rows_reach_eval_dataset` |
| 3-4 neither split contains the other | `test_no_validation_row_appears_in_the_training_dataset`, `…no_training_row…`, `…the_two_arms_share_no_row_inside_the_trainer` |
| 5-6 internal held-out rows not passed to `Trainer` | `test_an_internal_held_out_split_is_not_exportable_at_all`, `…no_internal_held_out_row_reaches_either_trainer_arm`, `…the_conversion_authority_refuses_a_held_out_id…` |
| 7 eval v1/v2 never involved | `test_the_evaluation_only_corpus_is_named_by_no_training_path`, `…an_ineligible_record_is_excluded…` |
| 8 production encoder | `test_validation_uses_the_same_encoder_and_tokenizer_as_training` |
| 9-10 max length respected, nothing truncates | `test_validation_respects_max_sequence_length`, `…truncation_count_is_reported_separately`, `…no_validation_row_truncates_at_512…`, `…a_cap_that_strands_every_supervised_token_is_refused` |
| 11 no rows or labels added to training | `test_validation_adds_no_row_and_no_label_to_the_training_dataset`, `…never_generates_during_validation` |
| 12 early stopping disabled | `test_early_stopping_remains_disabled` |
| 13 checkpoint saving disabled | `test_checkpoint_saving_remains_disabled`, `…checkpoint_writing_stays_refused_whatever_the_validation_cadence`, `…the_adapter_directory_gains_no_checkpoint_artifact` |
| 14 `load_best_model_at_end` false | `test_load_best_model_at_end_remains_false` |
| 15-16 behaviour is identity-bound, both directions | `test_enabling_validation_moves_the_config_identity`, `…disabling_validation_reproduces_the_pre_s3g2_identity`, `…moves_the_plan_identity`, `…the_plan_records_the_cadence_it_authorises` |
| 17 legacy configurations readable | `test_a_config_document_that_never_heard_of_validation_still_loads`, `…a_document_naming_the_field_round_trips`, `…an_unknown_validation_field_name_still_fails_closed` |
| 18 pre-S3G.2 run shape compatible | `test_the_pre_s3g2_run_shape_is_unchanged` |

Plus the readiness boundary in both directions, the observability record, the non-finite guard,
the export authority's own invariants, and that planning still imports no framework and creates
nothing.

**The framework surface is faked, and the fake is not the point of trust.** `_runtime()` is
replaced; every other line of `_train` runs, including the real `convert_sft_export`, the real
`_encode`, the real `build_labels` and the real `_masking_self_test`. The properties under test
are properties of the wiring — which object receives which rows under which arguments — and a
real 0.6B forward pass would establish none of them while costing minutes per test.

**The tests were verified non-vacuous.** With `eval_dataset=eval_rows` temporarily reverted to
`eval_dataset=None` — the exact pre-S3G.2 line — **5 of the 70 fail**, including
`test_the_validation_rows_reach_eval_dataset`. The line was then restored and all 70 pass.

### 13.1 Suite results

| Scope | Result |
|---|---|
| S3G + S3G.1 + S3G.2 regression files | **102 passed, 0 failed** (32 pre-existing + 70 new) |
| Focused M62 (`-k m62`) | **2755 passed, 25 skipped, 0 failed** (10m17s) |
| **Full inner suite** (`pytest tests -q -rs`) | **6701 passed, 50 skipped, 0 failed** (14m21s) |

Run **once**, near the end, because this milestone changes shared backend, config, planner and
export code — the condition the brief's test policy names. Every one of the 50 skips is
*named with its reason*, which S3G could not do: 6 optional-dependency module skips
(`fastapi` × 3, `chromadb` × 3), 1 voice profile, 17 MCP-only tool comparisons, 8
symlink/privilege cases, 4 `bandit is not on PATH`, 1 sealed-S3E.2-generation-absent, and 13
further host-privilege symlink cases inside M62 files.

**These counts are NOT reconciled against S3G's 6708 / 59, and no reconciliation is claimed.**
Collection moved by −16 while this milestone *added* 70 tests, so roughly 86 tests that
were collected in the S3G session are not collected here. The skip list shows why chasing that
is the wrong instinct: whole modules enter and leave collection depending on which optional
packages are importable on the host that day, and PROGRESS §15's standing warning covers
exactly this case. The M62 area — the only area this milestone touched — *is*
accounted for: 2684 → 2755 passed against 70 new tests. What is established is what was
measured: the whole inner suite passes at this commit with **0 failures and 0 errors**.

**On the M62 delta.** S3G recorded 2684 passed / 18 skipped; this session measures 2755 / 25.
Passed rose by 71 against 70 new tests, and skipped by 7. The new file contributes **70 passed,
0 skipped** when run alone, so one pass and seven skips come from elsewhere. PROGRESS §15's
standing warning applies and is not being worked around: skip sets are host- and
environment-dependent, and these counts must not be reconciled by arithmetic across sessions.
What is established is what was measured — the focused M62 selection passes at this commit with
**0 failures and 0 errors**.

### 13.2 Static gates

| Gate | Result |
|---|---|
| Ruff | **PASS** — over all 11 changed files |
| `compileall` | **PASS** — `training_gym`, `scripts`, `tests` |
| `git diff --check` | **PASS** |
| Secret scan (`core.redaction_policy.scan_for_leaks`) | **PASS** — one `reasoning` finding, pre-existing and untouched: the literal `<think` inside `build_training_corpus.py`'s invariant check that *forbids* it, plus the docstring describing that check. Operator ruling **H4** classifies reasoning markup as hygiene, not a security leak. Identical to what S3G recorded. |
| Host-path scan | **PASS** — no absolute host path, no Windows user path, no `/home/…`, no `/Users/…`, no cache location in any changed file or in this document |
| Bandit | **RUN.** It *is* installed in the suite interpreter (1.9.4, `bandit.exe` present in `.venv/Scripts/`) and runs via `python -m bandit`, so PROGRESS §15's *"not installed"* note is imprecise: the package is installed, it is simply **not on PATH** — which is why four `grader_checks` tests still skip with *"bandit is not on PATH"*. 141 findings over the changeset, **all LOW**: 137 × B101 (`assert` in the new test file, which is what pytest tests are made of) and 4 × B105 false positives on the literals `'False'`, `'<eos>'` and an estimate note. **Zero MEDIUM, zero HIGH.** |
| Runtime artefact exclusion | **PASS** — the validation export lands under the gitignored `training_gym_datasets/`; `git check-ignore` confirms. Nothing runtime is tracked. |

---

## 14. What was NOT done

- No training. No `trainer.train()`, no backend execution against a model, no adapter, no
  `adapter_model.safetensors`.
- No `TRAIN` token created and none consumed.
- No model weights loaded. The tokenizer was loaded for the bounded length audit; the model was not.
- No generation, no inference, no evaluation of a model, no `EVAL` token.
- No benchmark: no optimizer step, no evaluation pass, no forward/backward timing.
- No early stopping, and no callback that could stop a run.
- No checkpoint strategy change, no `load_best_model_at_end`, no adapter-manifest version move.
- No hyperparameter redesigned, no dataset redesigned, no corpus row modified.
- No D29 fix, no scorer change, no `SCORING_VERSION` move, no D28 work.
- No promotion, activation, registry mutation, role assignment or adapter merge.
- No mutation of run-004, the S3E.2 artefacts, `m62-defensive-eval` v1 or v2, or any historical
  result.
- No merge, tag, release or version bump.
- No dependency installed, no global environment change, no network contact.
- No filesystem sweep for the model cache.
- No absolute host path recorded in any tracked file.

---

## 15. Limitations

1. **Nothing here has been trained.** That the wiring is correct is established by 70 tests
   against the production objects; that it *trains well* is a claim only a run can support.
2. **The eval arm has never met a real model.** `_runtime()` is faked in the tests, so
   `Trainer`'s actual evaluation loop, its log-history key names on this transformers build, and
   the real per-evaluation runtime are **unmeasured**. The keys read (`eval_loss`, `epoch`,
   `step`, `eval_runtime`) are transformers' documented ones, and the code tolerates their
   absence rather than assuming it.
3. **The overhead estimate is a model, not a measurement** (§12), and it inherits S3G §9.2's
   single calibration point.
4. **Nine validation rows is a very small sample.** A movement in validation loss over nine rows
   is a weak signal, and the reason early stopping is refused. It is enough to see a gross
   train/eval divergence; it is not enough to rank two runs.
5. **Validation loss says nothing about eligibility.** Every S3G limitation survives intact: the
   corpus is synthetic and single-author, 107 training rows is small, semantic leakage checking
   has never run, the gates are counts rather than calibrated thresholds, D29 bounds what QG-1
   means, and D28 leaves tool calls unmeasured.
6. **The closing evaluation is one extra forward pass**, and it is a deliberate cost — see §6 for
   why the epoch cadence alone does not produce an end-of-run number under `max_steps`.
7. **D31 has never been exercised by a live training run.** Like D30 before it, it is proven by
   tests and by tracing the production path, not by a run it fixed.

---

## 16. S3H readiness

```
S3H_READY: YES
```

Every condition, checked rather than assumed:

| | Condition | Result |
|---|---|---|
| 1 | Validation correctly wired | PASS — 9 rows reach `eval_dataset`; 70 tests, 5 of which fail against the pre-fix line |
| 2 | Dataset unchanged | PASS — `9bbac2f0…`; train export and dataset reference unmoved; no row modified |
| 3 | Leakage remains clean | PASS — no row was touched, so the S3G verdict stands unaltered |
| 4 | TRAIN / VALIDATION disjoint, both directions | PASS — ∅ overlap, on disk and in the trainer's own objects |
| 5 | Internal held-out material excluded | PASS — 0 of 12 in either export; not exportable at all |
| 6 | `m62-defensive-eval` v1/v2 excluded | PASS — named in the executable source of no module on the path |
| 7 | Max length still qualified | PASS — 0 of 9 validation rows and 0 of 107 training rows truncate at 512 |
| 8 | Cache remains verified | PASS — `present`, root digest `40f747d2037e389b`, matching S3G.1 |
| 9 | Plan has zero blockers | PASS — `plan_blockers: []`, `is_executable: true` |
| 10 | Validation behaviour is identity-bound | PASS — moves the config and plan hash when enabled; reproduces `654393d8…` when not |
| 11 | Artefact safety intact | PASS — no checkpoint, no pickle-shaped file, allowlist and manifest version untouched |
| 12 | No early stopping | PASS — `EARLY_STOPPING: DISABLED`, no callback, none importable |
| 13 | No unsafe checkpoints | PASS — `save_strategy: no`, `load_best_model_at_end: false` |
| 14 | No token consumed | PASS — none created either |
| 15 | No training executed | PASS |

**`S3H_READY: YES` is a statement about preconditions, not an authorisation.** S3H requires
explicit operator authorisation that has not been given, and it must not begin automatically.

**What readiness still does not establish.** Nothing here says the candidate will improve
anything. It says the run will now be *observable while it happens*. Every S3G and S3G.1
limitation survives intact (§15).

---

## 17. Next

**M62 S3H — first quality-oriented live training run.** It requires only:

1. ~~the reviewed cache root and a zero-blocker plan~~ — see §11;
2. ~~confirmation that no training row truncates at 512~~ — **closed in S3G.1**, and the
   validation arm re-checked here (§8.1);
3. ~~a validation split the trainer actually reads~~ — **closed here**;
4. **explicit operator authorisation for live training** — *not given*;
5. a fresh plan and its single-use `TRAIN:<hash>` token, consumed exactly once — *not created*.

Then, separately authorised: the eligibility-grade paired evaluation under the S3G §12 contract,
judged against the S3G §6 gates, which were written down before any of it ran.

**S3H is not started, and must not begin automatically.**

**TRAINING NOT RUN.**
