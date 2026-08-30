# V69 M63 S4B — Candidate 005: the same dial, one step further

**Status:** candidate 005 is `DESIGNED_UNTRAINED`.
**Control plane:** V3, generation 17.
**Branch:** `jarvis-v69-m63-world-state`. `master` untouched.

This milestone designs one candidate and qualifies the runtime it would train in. It
trains nothing, evaluates nothing, promotes nothing and creates no authority.

---

## 0 — The one-line summary

A human ruled that candidate 005 may move exactly one dial against candidate 004:

```
primary_axis     learning_rate
reference        5e-5      (candidate 004, as measured)
ruled            2.5e-5    (candidate 005, by operator ruling S4B-001)
SCIENTIFIC_DIFF_COUNT   1
```

Everything else — corpus, base model, revision, tokenizer, chat template, rank, alpha,
dropout, module surface, epochs, optimizer steps, batch, accumulation, seed, precision,
device, sequence length, rendering policy, reasoning policy, validation policy and loss
objective — is inherited from candidate 004 **by reference, not by transcription**.

---

## 1 — What this milestone claims, and what it does not

**It claims.** That a fifth candidate identity exists; that its configuration differs from
candidate 004's in exactly one scientific field; that the difference is the one a human
ruled; that the training runtime has been rebuilt and qualified without model weights;
and that all of this is re-derivable from tracked artefacts.

**It does not claim.**

* That 2.5e-5 is better than 5e-5. The design predicts nothing. "Keeps candidate 004's
  gains", "loses them" and "moves nothing measurable" are all live outcomes.
* That training is the indicated remedy. The repository's standing body-free conclusion
  is unchanged:

```
RECOMMENDED_REMEDY        TOOLING
TRAINING_JUSTIFICATION    TRAINING_EXPERIMENTALLY_ALLOWED_NOT_PROVEN_NECESSARY
```

  A permitted experiment is not an indicated one, and a human authorising one is not the
  repository concluding it was needed. Both statements are preserved deliberately and
  neither is weakened by this milestone.
* That candidate 004 is superseded. Candidate 004 remains
  `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` with `HUMAN_DECISION = HOLD` and
  `PROMOTED = NO`. Candidate 005 is a NEW identity built against it, never a retry of
  it, a resume of it, a re-seed of it or a patch to it.

---

## 2 — The operator ruling

```
ruling_id                 S4B-001
decision_kind             HUMAN_OPERATOR_RULING
scope                     DESIGN_ONLY
subject_candidate         qwen3-06b-lora-quality-live-005
reference_candidate       qwen3-06b-lora-quality-live-004
primary_axis              learning_rate
reference_value           5e-5
ruled_value               2.5e-5
ruling_phrase_recorded    false
ruling_phrase_sha256      1dcec70e2024626f0be0134f076adc2abd92f4a4d0ed41ae2d2c4ae42d265d9e
record                    state/m62/rulings/0002-s4b-candidate005-learning-rate.json
```

The phrase itself is not stored, exactly as `S3U-001` did not store its own. What is
stored is a digest plus the **normalisation rule** that produced it, so a reader can
re-derive the digest instead of taking it on trust. This is deliberate and it is not a
weakening: the digest is not a secret and it cannot be spent. The repository already
holds that position for plan tokens — *"token silence is ceremony hygiene, not
cryptography: the confirmation string is a pure function of `plan_hash`, neither secret
nor unpredictable"* — and a ruling digest grants strictly less than a token does, which
is nothing.

**A ruling is evidence that a human decided, never a capability.** `PROSE_CANNOT_GRANT_
AUTHORITY` is unchanged. Training still requires a separate single-use `TRAIN:<plan-hash>`
the operator supplies out of band, and this document does not contain one.

### 2.1 The exact narrow supersession

Generation 16 `ruled_out[2]` said:

> creating eval-v7 or any replacement holdout, or designing, naming or configuring a
> candidate 005, without a separate explicit operator ruling

S4B-001 is that separate explicit operator ruling, and it supersedes **one clause of
one entry**:

| clause | after S4B-001 |
|---|---|
| designing, naming or configuring a candidate 005 | superseded, for candidate 005 only, prospectively, only to 2.5e-5 |
| creating eval-v7 or any replacement holdout | **UNTOUCHED — still barred** |
| any second look at eval-v6 | **UNTOUCHED — still barred** |
| using eval-v5 as eligibility evidence | **UNTOUCHED — still barred** |
| promotion, activation, registry mutation, merge, tag, release, version bump | **UNTOUCHED — still barred** |

The generation-16 entry is **not erased** and remains factual at the generation that made
it (`historical_entry_erased: false`). Generation 16 `ruled_out[6]` warned against reading
candidate 004's learning-rate permission as general; the same warning now applies to this
one, and generation 17 records it as its own entry. **This ruling does not generalise into
authority for a candidate 006, a sweep, a second seed, or a second value of this axis.**

---

## 3 — The scientific question

> Holding candidate 004's reviewed training configuration constant, what artefact results
> from reducing the learning rate from 5e-5 to 2.5e-5 for exactly one controlled training
> experiment?

The lineage has varied exactly one dial across four candidates, which is what makes this
a dose-response reading rather than a fresh hypothesis:

```
2e-4   candidate 001    measured
1e-4   candidates 002, 003   measured
5e-5   candidate 004    measured, ELIGIBLE_FOR_HUMAN_REVIEW, HOLD
2.5e-5 candidate 005    NOT MEASURED — this design
```

Three of the four points exist. The fourth is what this candidate would produce. Note the
risk runs in both directions and the design says so up front: an update small enough to
reduce how far the adapter moves the base model is also an update that may be too small to
retain the improvements candidate 004 measured. Both readings are informative about the
same hypothesis, and neither is predicted here.

---

## 4 — Identity and reference

```
candidate_id       qwen3-06b-lora-quality-live-005
ordinal            5
parent             qwen3-06b-lora-quality-live-004
experiment_name    m62-s4b-defensive-quality-005
option key         S4B          (used by candidate 005 and by nothing else)
status             DESIGNED_UNTRAINED
adapter_sha256     null
adapter_manifest   null
training_receipt   null
evaluation_corpus  null
evaluation_receipt null
production role    none — candidate 005 is INACTIVE
```

Candidates 001–004 keep the option keys, corpora, identities and digests they were
measured under. Adding `S4B` re-identifies nothing.

---

## 5 — The single axis, enforced rather than asserted

The dials are **derived** from candidate 004's option by dictionary expansion, so there is
no second place for them to drift to:

```python
OPTIONS["S4B"] = {
    **OPTIONS[S4B_REFERENCE_OPTION],          # S4B_REFERENCE_OPTION is CANDIDATE_OPTION["004"]
    CANDIDATE_005_PRIMARY_AXIS: CANDIDATE_005_LEARNING_RATE,
    ...
}
```

| dial | candidate 004 | candidate 005 |
|---|---|---|
| `learning_rate` | 5e-5 | **2.5e-5** |
| `lora_rank` | 16 | 16 |
| `lora_alpha` | 32 | 32 |
| `lora_dropout` | 0.05 | 0.05 |
| `weight_decay` | 0.0 | 0.0 |
| `warmup_ratio` | 0.1 | 0.1 |
| `epochs` | 2 | 2 |
| `max_steps` | 40 | 40 |
| `gradient_accumulation_steps` | 8 | 8 |

```
single_axis_diff("005")   frozenset({"learning_rate"})
SCIENTIFIC_DIFF_COUNT     1
```

**alpha is deliberately not slaved.** A learning-rate change needs no compensating
adjustment: `alpha/r` stays 32/16 = 2.0 because neither term moves. A "compensating"
second dial would be a second axis wearing a justification.

`verify_single_axis("005")` refuses the configuration — while it is being built, before a
plan hash exists and long before a token could be spent on it — if a second dial moves, if
the axis does not move at all, or if the axis moves to any value other than the ruled one.
The control-plane verifier re-derives the same relation independently from the same
generator.

### 5.1 What is inherited by reference, not retyped

`CANDIDATE_REASONING["005"] = CANDIDATE_REASONING["004"]` and
`TRAINING_DATASET_VERSION_005 = TRAINING_DATASET_VERSION_004`. D37 stays FIXED and is not
reopened as an axis: train/eval render parity is one object, not two values that happen to
agree.

---

## 6 — Training corpus: `m62-defensive-quality-train v2`, unchanged

```
dataset_id        m62-defensive-quality-train
version           v2
manifest_hash     24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f
record_count      182        (154 TRAIN · 12 VALIDATION · 8 HIDDEN_EVALUATION · 8 SECURITY_REGRESSION)
reference_hash    b3e1be3ed7e41953f874493a398c2dc3bd2267321d32d45572a5b4ba95f54a5c
```

Re-derived from the artefacts on disk by `load_manifest`, which re-hashes every shard and
refuses symlinks. `reference_hash` is byte-identical to the one candidate 004's sealed
training receipt carries, which is the check that the corpus is the same corpus and not a
rebuild that happens to have the same row count.

No `train-v3` exists and none was created. No row was added, deleted, rewritten,
reordered or rebalanced, and no split was changed. Candidates 002, 003 and 004 having
trained on `v2` does not prevent its reuse; changing it to create novelty would be a
second axis.

---

## 7 — The training runtime, rebuilt and qualified

This is a **compatibility recovery, not a scientific change**, and it is recorded
explicitly because it is a change to the execution environment.

The environment candidate 004 trained in, `.venv-m62-train-linux`, is a
`BROKEN_HISTORICAL_RUNTIME`: its `pyvenv.cfg` declares `version = 3.13.14` and
`executable = /usr/bin/python3.13`, the host no longer has a 3.13 interpreter at all, and
its `bin/python` now resolves to 3.14.6 while its packages remain under
`lib/python3.13/site-packages` where a 3.14 interpreter cannot see them.

**It was measured and not touched.** No `PYTHONPATH` was pointed at its 3.13 packages, no
`pyvenv.cfg` was edited, no symlink was retargeted, no binary extension module was copied
between interpreter versions, and it was not deleted.

```
OLD_BROKEN_VENV_MUTATED    NO
PYTHON_RUNTIME_STRATEGY    PY314_NATIVE
```

Every canonical pin was checked for cp314 distributions **before** anything was created:
`torch` has a `cp314` build on the official PyTorch CPU index carrying the exact
`+cpu` local version; `transformers`, `peft`, `datasets`, `trl`, `accelerate`,
`jsonschema` and `huggingface_hub` are `py3-none-any`; `safetensors` and `tokenizers` ship
stable-ABI `abi3` wheels; `numpy` and `sentencepiece` ship dedicated `cp314` wheels. No
version was changed to make an installation succeed.

A new project-local, gitignored environment was created from the host 3.14.6 interpreter
and the exact canonical versions installed into it — and nowhere else. No system Python
was modified, no OS package was installed, no `sudo` was used and no global or `--user`
environment was touched.

```
PYTHON_VERSION       3.14.6
torch                2.13.0+cpu        official PyTorch CPU index
transformers         5.14.1            PyPI
peft                 0.20.0            PyPI
datasets             5.0.1             PyPI
trl                  1.9.2             PyPI
accelerate           1.14.0            PyPI
safetensors          0.8.0             PyPI
tokenizers           0.22.2            PyPI
sentencepiece        0.2.2             PyPI
numpy                2.5.2             PyPI
jsonschema           4.26.0            PyPI
huggingface_hub      1.27.0            PyPI
PIP_CHECK            PASS — no broken requirements
```

The pins are tracked at `jarvis/requirements/training-m62-pinned.txt`. Two explicit
install commands, each against exactly one index, rather than one command with an
`--extra-index-url`: an extra index applies to every name in the file and resolves by
highest version across indexes, which is the dependency-confusion shape.

**The environment is the historical one where it can be measured to be.** Candidate 004's
root-bound `config_hash` re-derives byte-exact under the new runtime at the same roots:

```
candidate 004 config_hash   3b433c4958d016972ffdcf39b3f3ab86e40e5b915e8b8c13f8df16d5770218df
                            == the value its sealed S3V receipt carries
```

That is the same check S3V used to show it was training in the environment S3P had
qualified. What it establishes is that the configuration surface is identical; it does not
claim bit-identical numerics across interpreter versions, and this milestone does not
assert `deterministic_reproduction_claimed`, which candidate 004's receipt also records as
`false`.

### 7.1 Runtime Doctor

`core/runtime_doctor.py` could not be imported in a training environment at all — it
imported `loguru` at module scope, and a venv holding the pinned training backend and
nothing else has none. That is a real defect in the one module whose stated job is
diagnosing environments that are broken, and it was fixed under its own commit with its
own regression test before this design was written. The import is now guarded with a
standard-library fallback; no check, finding or behaviour changed.

Training-runtime findings in the new environment:

```
python.executable          PASS
python.environment_drift   PASS   import-path site-packages match the running 3.14 interpreter
python.venv                PASS
deps.training              PASS   full training stack present
```

Findings about the JARVIS **application** stack (`deps.core`, `config.settings`) are
`blocked` in that environment and are reported here rather than hidden: a training venv is
not an application venv and deliberately carries neither `pydantic` nor `loguru`. They are
unrelated to training-runtime qualification.

---

## 8 — Base model, tokenizer and rendering

```
BASE_MODEL           Qwen/Qwen3-0.6B
BASE_REVISION        c1899de289a04d12100db370d81485cdf75e47ca      immutable commit, never a branch or tag
tokenizer            Qwen/Qwen3-0.6B @ the same revision
chat_template_digest a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8
reasoning_policy     disabled       inherited from candidate 004 by assignment (D37)
precision / device   fp32 / cpu
LoRA scope           ATTENTION_AND_MLP — q,k,v,o,gate,up,down
seed                 42
model_download_policy DENY · local_files_only · trust_remote_code false
```

The reviewed model cache is present with exactly that revision. Its absolute path is
deliberately not recorded here; only its evidence digest travels.

---

## 9 — Authority: none of it is created here

```
TRAIN_AUTHORITY_CREATED     NO
TRAIN_AUTHORITY_CONSUMED    NO
EVAL_AUTHORITY              NONE
PROMOTION_AUTHORITY         NONE
MODEL_WEIGHT_LOADS          0
OPTIMIZER_STARTS            0
TRAINING_STEPS              0
TRAIN_ATTEMPTS              0
ADAPTER_ARTIFACTS           0
```

The plan hash is derived through token-silent surfaces only. The canonical executor's
`--dry-run` and `--print-plan` are **not** token-silent: both call
`TrainingPlan.to_record()`, which calls `confirmation_token()` and prints the literal
`TRAIN:<plan-hash>`. Two surfaces avoid that and are the only ones used before authority:

* `build_quality_training_config.py --plan` — computes `plan_hash()` and the blocker
  list, and never calls `to_record()` or `confirmation_token()`.
* `scripts/qualify_m62_train_runtime.py` — `--runtime-report` emits the deterministic,
  path-normalised runtime identity and its digest; `--plan` emits the immutable train
  plan binding the frozen source commit, the candidate and its parent, the ruling and its
  digest, the axis and both values, every dial that may not move, the dataset and base
  model identities, the runtime digest, the config digest, the output root, the expected
  artefact rules and receipt path, and the canonical `plan_hash`. It refuses outright if
  a dial that is not the axis has moved.

The property is asserted, not asserted about: focused tests monkeypatch
`confirmation_token`, `to_record` and `expected_effects` to raise, and require both
surfaces to still succeed and emit a plan hash. The qualifier's call graph is separately
checked to contain none of the three.

---

## 10 — Holdout firewall

```
eval-v4   USED_IMMUTABLE
eval-v5   FROZEN_UNUSED · spent_by null · RETIRED from eligibility use
eval-v6   USED_IMMUTABLE — spent by S3Y on candidate 004
eval-v7   NOT_CREATED, and this session is permanently disqualified from creating it
```

No holdout was opened, read, reconstructed, enumerated, summarised or used as training,
validation, debugging, qualitative-review or ablation material. `eval-v5` remains
`FROZEN_UNUSED` with `spent_by` null because **no model ever saw it**, and it stays
retired: it is not eligibility evidence for candidate 005 or any later candidate.

**A holdout author is never its evaluator.** The session that designs and trains
candidate 005 must not author its own exam. Candidate 005's evaluation therefore requires
a separate fresh independent session, a fresh holdout it did not write, and a separate
human roadmap ruling — none of which exist.

---

## 11 — Limitations

1. **The runtime is not bit-identical to candidate 004's.** It is the same package set at
   the same versions on a different CPython minor. Configuration identity is proved;
   numeric identity is not claimed, and no run in this lineage has ever claimed
   deterministic reproduction.
2. **`plan_hash` and `config_hash` are root-bound.** They bind `output_root_id`, a digest
   of a resolved absolute path, so they reproduce in one clone at one location and are
   re-derived on the executing host rather than pinned in the control plane.
3. **`plan_hash` binds the host's available-memory CATEGORY, and this host sits on the
   boundary.** `HardwareCapabilityReport.identity()` deliberately excludes the raw
   `available_ram_gb` and `output_disk_free_gb` — a token that expired between being
   printed and being typed would teach an operator to paste confirmations without reading
   them — but it keeps `available_ram_category`, because crossing a memory class is a real
   change that should invalidate a plan. Measured on this host, the two states are:

   ```
   available >= 8 GB   available_ram_category 8_to_16gb   plan_hash 5a786af9…e5403423
   available <  8 GB   available_ram_category under_8gb   plan_hash fa8f1dff…fe738384
   ```

   The machine currently reports 8 GB available, so it flips under load. **This is not
   plan nondeterminism:** two derivations in separate processes under the same host state
   are byte-identical, measured three times. It does mean an authorised token can go
   STALE, and the response to a stale token is to STOP and re-derive — never to train on
   a plan the token does not bind, and never to reconstruct the token for the operator.
4. **A training result would be an artefact, not a verdict.** Train and validation loss
   are diagnostic. `VALIDATION` is steering material and appears in no gate; it is never
   held-out eligibility evidence.
5. **`RECOMMENDED_REMEDY` is still `TOOLING`.** Nothing here re-ranks that.
6. **Runtime presence is host-local.** The absence of a run directory for candidate 005 is
   checked on this host and inherits the PARTIAL stale-state limitation.

---

## 12 — Exact next step

A single-use human `TRAIN:<plan-hash>` for candidate 005, supplied out of band. Without
it, nothing model-facing happens. With it, exactly ONE training run may execute — no
second seed, no second value of the axis, no candidate 005b, no candidate 006, no sweep,
no retry because a number looks bad.

After a successful run candidate 005 becomes `TRAINED_UNEVALUATED`: an artefact exists and
is scientifically unmeasured. Evaluation is a separate ceremony, in a fresh session, with
a fresh instrument, under fresh human authority.
