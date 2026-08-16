# V69 M62 S3P — candidate 003: one authority, one run, and the evidence that outlives it

> **Status: `CANDIDATE_003_STATE: TRAINED_UNEVALUATED`.** The operator authorised
> **exactly one** live training attempt for `qwen3-06b-lora-quality-live-003`. One
> plan-bound `TRAIN` capability was created, consumed once, and spent on one training
> process that completed all 40 preregistered optimizer steps and produced one verified
> LoRA adapter.
>
> **Zero evaluations, zero model generations, zero held-out reads, no `EVAL` authority,
> no promotion.** `m62-defensive-eval v4` remains **`FROZEN_UNUSED`**. Candidate 003's
> behaviour is **unknown, not estimated**.

| | |
|---|---|
| Milestone | V69 M62 **S3P** — candidate-003 live training + Control Plane generation 3 |
| Date | 2026-08-16 |
| Branch | `jarvis-v69-m62-training-gym` |
| Starting HEAD / training source commit | `bac49c4a49194d84fbc7f61656662fdcd54799ca` |
| Master | `3705114228edef2f665be349c5c4429b7b16777a` (untouched) |
| Host | Kali Linux mini-PC, CPU only |
| Preceding | **S3O** — `V69_M62_S3O_CANDIDATE003_CONTROLLED_DESIGN.md` |

---

## 1 — Authorisation and boundary

The operator authorised **one potential live TRAIN attempt**, conditional on a complete
pre-authority gate passing first. The authorisation was explicitly *not* "train no matter
what": it permitted one fresh plan-bound capability, one consumption, one process, up to
the preregistered 40 optimizer steps, the model-weight loading that requires, `train-v2`
diagnostic validation, one verified adapter, post-run evidence, and generation 3 **if and
only if the evidence supported it**.

Measured rather than asserted:

```
BACKGROUND_AGENTS_LAUNCHED:            0
CONCURRENT_WRITERS:                    0
TRAIN_TOKEN_CREATED / CONSUMED:        YES / YES   (1 / 1)
SECOND_TRAIN_TOKEN_CREATED:            NO
LIVE_TRAIN_ATTEMPTS:                   1
RETRY_AUTHORIZED:                      NO
EVAL_TOKEN_CREATED / CONSUMED:         NO / NO
EVAL_PLAN_CREATED:                     NO
MODEL_WEIGHTS_LOADED:                  YES  (training only)
MODEL_GENERATIONS:                     0
MODEL_RESPONSE_TOKENS_GENERATED:       0
HELDOUT_EVALUATION_RUNS:               0
EVAL_V4_BODY_READ:                     NO
FULL_HISTORICAL_PROGRESS_READ:         NO
TRAIN_V2_MODIFIED / TRAIN_V3_CREATED:  NO / NO
D37 / D38 / D39:                       FIXED / FIXED_OBSERVABILITY_ONLY / OPEN, unchanged
GATES / GRADERS / THRESHOLDS:          UNCHANGED
MERGE / TAG / RELEASE / VERSION_BUMP:  NO / NO / NO / NO
```

**S3P is an execution milestone.** It spent a capability. It did not measure a model.

---

## 2 — Single-orchestrator rule

One Claude orchestrator ran every step from pre-authority verification through the
terminal outcome. No background agent was launched, no work was delegated, and no second
writer touched the worktree or the control plane. `git worktree list` reported exactly one
worktree throughout, and `git stash list` was empty.

---

## 3 — Starting Git authority

Verified read-only before anything was read or written.

| | expected | measured |
|---|---|---|
| branch | `jarvis-v69-m62-training-gym` | same |
| HEAD | `bac49c4a49194d84fbc7f61656662fdcd54799ca` | same |
| `origin/…-training-gym` | same SHA | same |
| divergence | `0 0` | `0 0` |
| `master` | `3705114228edef2f665be349c5c4429b7b16777a` | same |
| worktree | CLEAN | clean, 0 staged, 0 unstaged, 0 untracked |

---

## 4 — Control Plane first

Run **before** any project reading, per the bootstrap contract:

```
python jarvis/scripts/verify_m62_control_plane.py
M62_CONTROL_PLANE_VERIFY: PASS      PROBLEMS: 0      (13/13 categories)
```

| | expected | measured |
|---|---|---|
| `SCHEMA_VERSION` | `m62.control_plane.1` | same |
| `STATE_GENERATION` | 2 | 2 |
| generation-2 snapshot | `state/m62/snapshots/0002-…-designed-untrained.json` | same |
| generation-2 SHA256 | `cdff52ee…fef3b621` | re-hashed from bytes, matches |
| generation-2 parent | `a2659d1f…25dcf2c3` | re-hashed from bytes, matches |
| candidate 003 | `DESIGNED_UNTRAINED` | same |

---

## 5 — Bootstrap surface, and the archive that stayed shut

```
FULL_HISTORICAL_PROGRESS_READ:   NO
HISTORY_ESCALATION_REQUIRED:     NO
```

Read: `current.json`, the generation-2 snapshot, the compact `PROGRESS.md`, the S3O deep
authority, and the canonical TRAIN/execution authority actually needed to operate the
mechanism (`build_quality_training_config.py`, `train_experiment.py`, `execution.py`,
`plan.py`, `run_store.py`, `artifacts.py`, and the S3K live-training record for the
canonical execution shape and the structural adapter control).

The 516,784-byte archive was never opened. Not one fact required escalation.

---

## 6 — The `eval-v4` firewall

```
EVAL_V4_BODY_READ:      NO
EVAL_V4_STATUS:         FROZEN_UNUSED
HOLDOUT_FIREWALL:       PASS
```

`build_evaluation_corpus.py` — the file holding the `v4` material builder and the authored
`v4` prompts and targets — was never opened. Neither was any task pack, promoted shard,
materialised pack or evaluation output directory. No `EVAL` plan was derived and no `EVAL`
capability exists. This document deliberately does not name the body-source symbol, so it
is safe to scan as well as safe to read.

**Training does not spend the holdout.** Candidate 003 is trained and `v4` is still fresh;
those are two independent facts and S3P kept them that way.

---

## 7 — The design, re-derived rather than read

Every value below was produced by the tracked production generator on this host, not
quoted from S3O and accepted.

```
CANDIDATE                qwen3-06b-lora-quality-live-003
EXPERIMENT               m62-s3o-defensive-quality-003
BASE / REVISION          Qwen/Qwen3-0.6B @ c1899de289a04d12100db370d81485cdf75e47ca
TRAIN CORPUS             m62-defensive-quality-train v2, manifest 24ceb1e0…
REASONING POLICY         DISABLED          LORA SCOPE   ATTENTION_AND_MLP
r / alpha / dropout      16 / 32 / 0.05    scaling 2.0  bias none  CAUSAL_LM
TARGET MODULES           q_proj k_proj v_proj o_proj gate_proj up_proj down_proj   (7)
LR / EPOCHS / MAX_STEPS  1e-4 / 2 / 40     warmup 0.1   weight decay 0.0
BATCH x GRAD ACC         1 x 8 = 8         SEED 42      MAX SEQUENCE 512
DEVICE / PRECISION       cpu / fp32        dataloader workers 0
CHECKPOINTING            no                gradient checkpointing false
VALIDATION               epoch (diagnostic) + closing evaluate()
EARLY STOPPING           disabled          load_best_model_at_end   false
DOWNLOAD POLICY          deny              trust_remote_code        false
```

| identity | S3O reference | re-derived here |
|---|---|---|
| `config_hash` (003) | `6f9f470f…e599e1f` | **identical**, and identical across two rebuilds |
| `config_hash` (002, control) | `08be37d3…9e608649` | **identical** to the S3J/S3K sealed value |
| `config_hash` (001) | `e80e04e4…4555c323` | identical to the S3M.1 value for this root |
| `output_root_id` | `1dd79ac5…d7c62c5ac` | identical |
| `plan_hash` | `414ce9e3…905f2a986` | **identical** |

`config_hash` and `plan_hash` are **root-bound**. They matched because this is the same
checkout and the same output root S3O derived them on; on any other host they must be
re-derived, never pasted.

### 7.1 The control, and the exact diff

Candidate 002 was rebuilt from the same generator against the same roots, and the two
canonical bodies were compared field by field over 43 top-level keys.

| key | 002 | 003 | class |
|---|---|---|---|
| `reasoning_policy` | **ABSENT** (= `MODEL_DEFAULT`) | `"disabled"` | **PRIMARY AXIS** |
| `run_id` | `…-002` | `…-003` | identity |
| `experiment_name` | `m62-s3j-…-002` | `m62-s3o-…-003` | identity |
| `notes` | S3J text | S3O text | provenance |

```
PRIMARY_EXPERIMENTAL_AXIS_COUNT:   1
PRIMARY_EXPERIMENTAL_AXIS_KEYS:    ["reasoning_policy"]
UNINTENDED_TRAINING_CONFIG_DIFFS:  0
CANDIDATE_OPTION["003"] == CANDIDATE_OPTION["002"]:   True  ("S3J")
```

Every other key is byte-identical, including all of `dataset_reference`, `lora`,
`resource_policy`, `output_root_id`, `created_at_utc` and `seed`. The dials are shared by
**reference**, not copied, so "every hyperparameter is identical" is one equality rather
than eight comparisons that could each rot.

---

## 8 — D37: the representation actually executed

```
CHAT_RENDER_POLICY_HASH (003, DISABLED)        8619f96c…a568490db0   == S3M.1 / S3O
CHAT_RENDER_POLICY_HASH (002, MODEL_DEFAULT)   892e003d…9db898a55
FULL_SEQUENCE_RENDER (003)                     c5e83324…d12a1c6e0
TEMPLATE_DIGEST                                a55ee1b1…9cf1974d8   (re-hashed from the
                                                                     reviewed tokenizer)
TRAIN_EVAL_PARITY_REASONING_POLICY is ELIGIBILITY_REASONING_POLICY   True
                                    is ReasoningPolicy.DISABLED      True
candidate_reasoning_policy("003") is the shared parity constant      True
MODEL_DEFAULT.template_kwarg  None      DISABLED.template_kwarg  False
```

The digests were re-derived, not pasted, and `892e003d… != 8619f96c…` is the measured
statement that the axis moves something.

**The axis reached the real run.** The adapter's own `backend_result.json` records
`chat_render_policy_hash: 8619f96c…` and `enable_thinking: false`. This is the first
M62 training run ever executed under the D37 fix.

---

## 9 — `train-v2`, masking and truncation

```
m62-defensive-quality-train v2   manifest 24ceb1e0…   parent 9bbac2f0…
182 records = 154 TRAIN + 12 VALIDATION + 8 hidden_evaluation + 8 security_regression
TRAIN export       82780fa0…  154 rows  file 72065595…   verifies, 0 problems
VALIDATION export  ac065112…   12 rows  file 7ee612ef…   verifies, 0 problems
dataset reference  b3e1be3e…
TRAIN_V2_MODIFIED  NO      TRAIN_V3_CREATED  NO      rows added  0      rebalance  NO
```

Tokenizer only, from the reviewed offline cache. **No weights were loaded for this check.**

```
ROWS_TESTED                       154 TRAIN + 12 VALIDATION = 166
ROWS_TRUNCATED                    0 / 166        (both policies, both splits)
MASKING                           PASS           manual_label_masking(-100), 0 problems
TERMINATOR_SUPERVISED             YES            <|im_end|> in every label vector
VALIDATION_CONTRIBUTES_GRADIENTS  NO
<think> in supervised span        MODEL_DEFAULT 154/154   ->   DISABLED 0/154
prompt_length delta               +4 tokens, every row, both splits
full-sequence input_ids           byte-identical between the two policies
TRAIN lengths min/median/max      65 / 113 / 169
VALIDATION lengths                90 / 109 / 155
```

The 512 ceiling truncates nothing, with the longest row at 169 tokens.

---

## 10 — Runtime and cache, requalified directly

`STALE_STATE_DETECTION` is `PARTIAL`, so generation 2 was not trusted for runtime
readiness. `.venv-m62-train-linux` was re-probed read-only. **Nothing was installed,
upgraded or removed, and `.venv-m62-eval-linux` was never touched.**

```
Python 3.13.14 · torch 2.13.0+cpu · transformers 5.14.1 · peft 0.20.0 · datasets 5.0.1
trl 1.9.2 · accelerate 1.14.0 · safetensors 0.8.0 · tokenizers 0.22.2
sentencepiece 0.2.2 · numpy 2.5.2 · jsonschema 4.26.0 · huggingface_hub 1.27.0

RUNTIME_DRIFT vs S3O:  ZERO   (all 12 packages + the interpreter)
CUDA available: False    torch default dtype: torch.float32
build_dependency_report(TRAINING, SFT_LORA) -> ready True, blockers []
```

Cache, located through the repository's own `probe_cache` rather than a filesystem sweep:

```
probe_cache status     present
probe_cache evidence   f399355ef441e8ec…   == the digest S3G.1 recorded
revisions cached       exactly c1899de289a04d12100db370d81485cdf75e47ca
weights present        model.safetensors
tokenizer assets       tokenizer.json · tokenizer_config.json · vocab.json · merges.txt
```

The absolute cache path is deliberately not recorded here — only its evidence digest.

Every command ran under `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`,
`HF_HUB_DISABLE_TELEMETRY=1`, with `local_files_only=True` and `trust_remote_code=False`.

---

## 11 — Resources, exclusivity and the plan

```
disk free (output filesystem)   168 GB      plan requires 0.406 GB
memory available                 9.4 GB     plan peak estimate 3.817 GB
CPU                              6 cores    load average 1.40
output roots writable            YES
```

Exclusivity, before any capability existed:

```
existing candidate003 training process   NONE      candidate003 adapter        NONE
candidate003 run directory               NONE      candidate003 ledger entry   0 of 6
existing TRAIN authority for this plan   NONE      other M62 training process  NONE
git worktrees                            1         stash entries               0
```

**Plan, derived twice through independent production paths:**

| path | config origin | plan hash |
|---|---|---|
| A — generator | built in code by `build_config()` | `414ce9e3b8adcddbc78aa9263e2a1fc8178e83a58d14db95607f143905f2a986` |
| B — document round-trip | `load_training_config()` on the emitted JSON, then `plan_training()` | **identical** |

```
PLAN_BLOCKERS   0
PLAN_WARNINGS   1  — "a CPU smoke run is slow; this validates the pipeline, and it is
                      not a route to a production adapter"   (reported, not suppressed)
plan_is_executable  true      performs_training/creates_adapter/contacts_network  false
selected device/precision  cpu / fp32       effective batch  8
```

Every substantive binding reproduced candidate 002's where it should — dataset reference
`b3e1be3e…`, hidden `b321d52c…`, security regression `705d5aed…`, base identity
`9701f4f3…`, tokenizer identity `45894db9…`, dependency `b6f206c7…`, hardware `6b717507…`,
feasibility `31dc790e…`, resource policy `96c06400…` — and `config_hash` differs, which is
the axis.

**Expected optimizer steps, re-derived from the verified export:** 154 TRAIN rows at
effective batch 8 gives `ceil(154/8) = 20` steps per epoch; 2 epochs lands exactly on 40,
which is the config's `max_steps`. The budget is arithmetic, not a guess.

---

## 12 — Ledger baseline, captured before the capability existed

```
training_runs.jsonl   6 lines · 1667 bytes · sha256 98068b00…98bf5dad94
run ids               smoke-live-003 (started, failed) · quality-live-001
                      (started, completed) · quality-live-002 (started, completed)
candidate003 events   0
runs present          quality-live-001, quality-live-002
quarantine            qwen3-06b-lora-smoke-live-003-7e9b2593 (historical residue)
```

---

## 13 — Pre-token test gate

```
invocation   pytest -k m62 --ignore=tests/test_live_brain_v61.py     (from jarvis/)
result       3341 passed · 20 skipped · 0 failed
```

Exactly the authoritative S3O baseline. The two documented tokenizer skips were not
"fixed" as a rider, and D39 was not touched.

---

## 14 — Execution freeze and the source fingerprint

After the tests and before any capability existed, the milestone entered **execution
freeze**: no tracked file was permitted to change until the run reached a terminal state
and post-run verification finished.

An execution-critical source fingerprint was derived from the **real import graph** —
the modules the training call path actually loads, intersected with the tracked file set.

```
EXECUTION_CRITICAL_FILES                68   (candidate config · training plan ·
                                             authority/token handling · execution ·
                                             backend · chat rendering · adapter artifact
                                             validation · dataset binding, transitively)
EXECUTION_CRITICAL_SOURCE_DIGEST_PRE    e6ff9dce699aca3abb77d8d63461eafd172144da8efee86407150d1d2cc5b08c
SOURCE_GIT_COMMIT                       bac49c4a49194d84fbc7f61656662fdcd54799ca
SOURCE_GIT_TREE_OID                     978dfc17aee7a97be6796b14a2fbf602a404bb6f
```

This digest is **evidence, not a new authority**. It adds no tracked file.

Immediately before capability creation, `current.json` was re-read: still generation 2,
still snapshot `cdff52ee…`. One writer, unmoved.

---

## 15 — The GO / NO-GO gate

All forty mandatory gates G01–G40 passed: git authority, control-plane verification,
generation, candidate state, design re-derivation, single-axis proof, the sole axis being
`reasoning_policy`, `DISABLED`, `ATTENTION_AND_MLP`, no other hyperparameter moved,
`train-v2` immutable, no `train-v3`, zero runtime drift, cache verified, D37 render
identity, masking, `0 / 166` truncation, supervised terminator, plan A ≡ plan B, zero
blockers, a clean focused suite, no existing adapter/run/ledger entry/capability, no
concurrent training process, both ledger baselines captured, the source fingerprint
captured, no source write after the tests, `eval-v4` unread and `FROZEN_UNUSED`, no `EVAL`
authority, and no network dependency.

```
PRE_TOKEN_GO_NO_GO:  PASS   (40 / 40 mandatory)
```

**Only at this point did the operator's authorisation become actionable.**

---

## 16 — The capability

```
TOKEN_FORM                   TRAIN:<plan-hash>
BOUND_PLAN_HASH              414ce9e3b8adcddbc78aa9263e2a1fc8178e83a58d14db95607f143905f2a986
TRAIN_TOKEN_CREATED          YES
TRAIN_TOKEN_CREATION_COUNT   1
TRAIN_TOKEN_CONSUMED         YES   (exactly once)
TRAIN_TOKEN_CONSUMPTION_COUNT 1
TRAIN_TOKEN_LITERAL_TRACKED  NO
SECOND_TRAIN_TOKEN_CREATED   NO
RETRY_AUTHORIZED             NO
```

The token is **derived** from the plan by `TrainingPlan.confirmation_token()`, never
issued by one; computing a plan spends nothing. Exactly one was derived, through the
production CLI's own `--print-plan` path so that the string the execution path recomputes
against is the string that was typed.

The derived plan hash was compared to the reviewed S3O plan **before** the capability was
extracted, so a drifted plan would have aborted with no capability in existence.

**Secrecy.** The literal was never printed, never echoed, never written to a file, never
placed in a document, a commit message or state JSON. It lived in one shell variable and
in the child process's argv, which is what the CLI requires. `set -x` was not enabled, no
environment dump was taken, no shell history was inspected, `/proc/<pid>/cmdline` was
never read, and no `ps` mode printing full command lines was used. The verifier's own
tracked-token scan passes over the whole tree.

Consumption is proven by artefacts rather than asserted: `training_log.jsonl` carries
`plan_consumed` in state `starting`, the run record's history contains
`preflight_verified -> starting  (reason: plan consumed)`, and the ledger gained exactly
one `started` line for this run id against this plan hash.

---

## 17 — The run

**One process. No fallback command, no second invocation, no retry.**

```
TRAINING_RESULT              SUCCESS
RUN_ID                       qwen3-06b-lora-quality-live-003
PROCESS_START (UTC)          2026-08-16T20:28:50Z
PROCESS_END   (UTC)          2026-08-16T20:53:54Z
WALL CLOCK                   25 min 04 s   (backend duration 1502.431 s)
EXIT CODE                    0
MAX_SIMULTANEOUS_CANDIDATE003_TRAIN_PROCESSES   1
OPTIMIZER STEPS  planned/attempted/completed    40 / 40 / 40
REALISED EPOCHS              2.0   (exactly, as designed)
CONVERTED RECORDS            154        TRUNCATED RECORDS   0
BACKEND STATUS / WARNINGS    succeeded / none
RUN STATE                    completed · interrupted false · error_category none
```

States visited: `created → config_validated → dataset_verified → dependencies_verified →
hardware_verified → planned → awaiting_confirmation → preflight_verified → starting →
running → artifact_validation → completed`.

Realised epochs are exactly 2.0 for the same arithmetic reason S3K recorded: 20 optimizer
steps per epoch means a 40-step budget lands precisely on the epoch-2 boundary.

The run was launched detached so that a tool or UI timeout could never be mistaken for a
model-run failure, and the **same** process was observed by PID and by canonical run state
throughout. No second process was ever started.

### 17.1 Losses

Aggregate reported by the trainer: **`train_loss = 3.408492`**.

| step | epoch | training loss |
|---:|---:|---:|
| 5 | 0.2597 | 3.968051 |
| 10 | 0.5195 | 3.619793 |
| 15 | 0.7792 | 3.673562 |
| 20 | 1.0000 | 3.359027 |
| 25 | 1.2597 | 3.141880 |
| 30 | 1.5195 | 3.148657 |
| 35 | 1.7792 | 3.062798 |
| 40 | 2.0000 | 3.294167 |

The curve falls 3.968051 → 3.062798 and is **not monotone**, rising at steps 15, 30 and at
the final step 40. That is ordinary batch-to-batch variation, and the run was not touched
because of it: the frozen plan governs, never the shape of an intermediate number.

```
VALIDATION_STRATEGY               EPOCH_PLUS_FINAL      VALIDATION_ROWS  12
PERIODIC_VALIDATION_EVALUATIONS   2                     ROWS TRUNCATED   0 / 12
GENERATION_DURING_VALIDATION      NO  (teacher-forced loss only)
VALIDATION_CONTRIBUTES_GRADIENTS  NO
NON_FINITE_METRICS                0
EARLY_STOPPING / CHECKPOINTS / LOAD_BEST   disabled / disabled / false
```

| # | epoch | step | eval loss | eval runtime |
|---:|---:|---:|---:|---:|
| 1 | 1.0 | 20 | **3.250574** | 12.1111 s |
| 2 | 2.0 | 40 | **3.178661** | 50.6168 s |
| final `evaluate()` | 2.0 | — | **3.178661** | **39.4033 s** |

Two periodic evaluations fired and the closing pass is the third — the same arrangement
S3K saw, and for the same reason. The closing pass measured the same weights as the
step-40 evaluation and returned the same loss with a **visibly different runtime**, which
is what shows they are two passes and not one number printed twice.

### 17.2 What the losses are not

**VALIDATION is train-side steering material** (`TRAIN_SIDE_SPLITS`), it appears in no
gate, and it authorises nothing.

```
VALIDATION_EVALUATIONS:     2 periodic + 1 closing
HELDOUT_EVALUATION_RUNS:    0
```

It is **not** "held-out evaluation", **not** candidate eligibility and **not** a quality
result. Comparing candidate 003's numbers to candidate 002's is not meaningful either:
different training representation, and — when it is eventually measured — a different
exam. **Candidate 003's behaviour is unknown, not estimated.**

Nothing here says the D37 fix restored stopping, repaired structured output or improved
safety. D37's historical causality remains `NOT_ESTABLISHED`.

---

## 18 — Ledger deltas

```
candidate003 authority creations     +1
candidate003 authority consumptions  +1
candidate003 training attempts       +1
candidate003 completed runs           1
unrelated candidate TRAIN deltas      0
duplicate plan / token / run entries  0
ledger lines                          6 -> 8   (started + completed, plan 414ce9e3…)
```

The ledger gained exactly two lines, both bound to this plan hash. No other run id
received an event.

---

## 19 — Artefacts and verification

```
ADAPTER_CREATED             YES
COMPLETED_RUN_VERIFIER      PASS   (verify_completed_run -> 0 problems)
ADAPTER_VERIFIER            valid  (validate_adapter_directory -> 0 problems)
SAFETENSORS_ONLY            YES    PICKLE_ARTIFACTS        0
CHECKPOINT_DIRECTORIES      0      NESTED_DIRECTORIES      0
SYMLINKS                    0      HARDLINKED FILES        0
EXECUTABLE BITS             0      BASE_MODEL_DUMP         NO
NETWORK_OPERATIONS          0
```

| Digest | Value |
|---|---|
| **`ADAPTER_SHA256`** | `6ccd8fdc16c6f79d5d7965c1d30a42faecc226581a20f701c582588c76ce4ea6` |
| **`ADAPTER_MANIFEST_HASH`** | `3bf56ff85538a2651341ec45f48281f3a7c7e861d16b6260b3a686fe0fb5249f` |
| **`ARTIFACT_SET_HASH`** | `148e3ef15e9e3890e25f83ad1b7361192f08ed92c89741a043e4f3985cbf83da` |
| `ADAPTER_BYTES` | 40,422,168 (run directory total 40,434,133) |

The run directory is flat: `adapter_model.safetensors`, `adapter_config.json`,
`README.md`, `backend_result.json`, `run.json`, `training_log.jsonl`, plus
`adapter-manifest.json`. No file is present that the manifest does not name, every file
re-hashes to its recorded digest and size, and the tree digest re-derives from the bytes.

### 19.1 Tensors

```
Tensor count           392        lora_A / lora_B      196 / 196
Non-LoRA tensors       0          dtype                F32 only
Adapter parameters     10,092,544 Total parameters     606,142,464
Trainable share        1.665%     Non-finite tensors   0 of 392
All-zero tensors       0
Adapted projections    down_proj gate_proj k_proj o_proj q_proj up_proj v_proj  (all 7)
```

392 reconciles exactly: 28 layers × 7 projections × 2 matrices. The safetensors header and
the backend's own trainable-parameter report agree.

### 19.2 Structural control against candidate 002

Re-derived from candidate 002's **real adapter on disk** — the artefact S3K produced — not
copied from prose.

| property | candidate 002 | candidate 003 | verdict |
|---|---|---|---|
| tensor count | 392 | 392 | match |
| `lora_A` / `lora_B` | 196 / 196 | 196 / 196 | match |
| non-LoRA tensors | 0 | 0 | match |
| adapter parameters | 10,092,544 | 10,092,544 | match |
| total parameters | 606,142,464 | 606,142,464 | match |
| adapted projections | the seven | the seven | match |
| dtype | F32 | F32 | match |
| file set | identical | identical | match |
| adapter bytes | 40,422,168 | 40,422,168 | match |
| **adapter SHA256** | `319c2524…` | `6ccd8fdc…` | **differ — as required** |

```
STRUCTURAL_CONTROL_MATCH:  PASS
```

Same architecture, same scope, therefore the same structure; different data
representation, therefore different learned values. Identical *size* is a property of the
shape, not of the contents.

**Predecessors untouched.** Candidate 001 re-hashes to `43213035…` and candidate 002 to
`319c2524…`, both unchanged.

### 19.3 Nothing private was written

A scan of every file in the run directory found no absolute host path, no `/home/…`, no
`/Users/…`, no Windows user path, no cache location, no username, no token literal and no
raw dataset row. All of it is gitignored and none of it is tracked.

---

## 20 — Post-run source freeze recheck

```
HEAD                                    bac49c4a49194d84fbc7f61656662fdcd54799ca
SOURCE_GIT_TREE_OID                     978dfc17aee7a97be6796b14a2fbf602a404bb6f
tracked worktree                        CLEAN
EXECUTION_CRITICAL_SOURCE_DIGEST_POST   e6ff9dce699aca3abb77d8d63461eafd172144da8efee86407150d1d2cc5b08c
EXECUTION_CRITICAL_SOURCE_DIGEST_MATCH  YES   (PRE == POST, same 68 files)
SOURCE_CHANGED_DURING_TRAIN             NO
```

Not one tracked byte moved between the GO decision and the verified terminal result. The
adapter was produced by exactly the source S3O reviewed.

---

## 21 — The portable receipt, and why one had to exist

After the run, the control plane could not honestly record `TRAINED_UNEVALUATED`. The
verifier's `check_candidate_state` had no arm for the state at all, and the only surfaces
that could have carried it were the snapshot and a constant in the verifier — two
writable files edited by the same hand in the same commit. That is the exact
self-fulfilling shape the zero-trust design exists to prevent:

> the snapshot says `TRAINED_UNEVALUATED`, a constant says `TRAINED_UNEVALUATED`, they
> agree, therefore PASS.

Everything that actually proves the run happened — the adapter, its manifest, `run.json`,
`backend_result.json`, the ledger — is **gitignored**. That is the right home for weights
and the wrong home for history: a fresh clone has none of it.

So S3P added the minimum missing piece:

```
state/m62/receipts/qwen3-06b-lora-quality-live-003.train.json   (5,308 bytes, tracked)
state/m62/schema/m62-train-receipt.schema.json                  (the contract)
jarvis/scripts/build_m62_train_receipt.py                       (the derivation)
```

**Derived, never typed.** Every field is distilled by the tracked builder from the run's
own artefacts and from the production generator. The serialiser is the repository's single
`canonical_json`, so rebuilding from the same evidence reproduces the same bytes — which a
test asserts.

The receipt carries **no token literal, no absolute path, no dataset row, no model output,
no `eval-v4` material, no timestamp and no self-referential digest**. Its identity is its
bytes; the snapshot that points at it records the digest, so the two can never agree with
each other by construction.

```
RECEIPT_SCHEMA          m62.train_receipt.1
BUILTIN VALIDATOR       0 problems       JSONSCHEMA (second opinion)  0 problems
model_cache_evidence    f399355e…   root-independent: it digests the cache DIRECTORY
                                    NAME, which derives from the model id and nothing
                                    else, and reproduces the canonical probe exactly
```

### 21.1 Runtime artefact versus historical fact

```
TRAINING_HISTORICALLY_VERIFIED:    YES
CURRENT_RUNTIME_ADAPTER_PRESENT:   YES  (and it verifies)
```

These are deliberately two statements. In a future clean clone the second may be `NO`
while the first stays true, sealed by the receipt. Persistent candidate history must never
depend forever on ignored runtime files, and after S3P it does not.

---

## 22 — The verifier extension, made only after the run

`check_training_receipt` was added **after** the terminal result, never before, and it
changes no training behaviour, weakens no existing check and alters no generation-1 or
generation-2 semantics. A `TRAINED_UNEVALUATED` claim is now refused unless:

* the snapshot names a receipt, and the receipt is a tracked regular file;
* it validates against the published schema, is ASCII, and carries no token literal, no
  private path, no body-source symbol and no holdout task id;
* it describes **this** candidate, and the production generator names that run id;
* its base model and revision match the snapshot **and** the generator's pinned constants;
* its corpus matches the snapshot's and hashes to the sealed `FROZEN_DATASETS` manifest,
  at the version the generator designs;
* its reasoning policy is what the generator resolves, and that is `DISABLED`;
* its render identity equals the one **re-derived from the snapshot's own template
  digest** — a string-only derivation that loads no tokenizer;
* authority creations == 1 and consumptions == 1, bound to the plan the run executed, and
  named by the ledger;
* the terminal status is `SUCCESS`, uninterrupted and complete;
* planned and completed optimizer steps both equal the **design's** `max_steps`, and the
  configured epochs equal the design's epochs — re-derived from `OPTIONS`, not restated;
* a closing validation pass is present, the loss count matches the evaluation count, and
  every metric is finite;
* adapter SHA, manifest hash and artifact-set hash are present, and the first two equal
  the snapshot's;
* the structural fingerprint equals the sealed control, and the target modules are the
  `attention_and_mlp` set;
* both canonical verifiers passed and no checkpoint tree exists;
* no held-out evaluation, no `EVAL` authority and no evaluation corpus is claimed;
* both recorded commits exist in this repository.

An import failure is a **failure**, never a skipped pass. Runtime presence of the adapter
is reported as an observation and is deliberately **not** required.

`check_candidate_design` was widened from `DESIGNED_UNTRAINED` to also cover
`TRAINED_UNEVALUATED`. Filtering on the designed state alone would have silently stopped
checking the single-axis claim at the exact moment it began to bind real weights. The
three runtime-absence assertions remain `DESIGNED_UNTRAINED`-only, because a trained
candidate legitimately has a run directory and a ledger entry.

The strict validator gained a `number` type, because a loss is a float. It matches the
`jsonschema` second opinion, which the verifier still requires to agree.

---

## 23 — Control Plane generation 3

Two-phase, so the state can never describe itself.

```
PHASE A   the receipt, its schema, its builder, this document, the S3P suite and the
          verifier extension.                              ->  subject commit
PHASE B   the generation-3 snapshot, current.json, PROGRESS, the history index.
```

Between the phases the verifier **deliberately fails** `CANDIDATE_STATE`: the snapshot
still says `DESIGNED_UNTRAINED` while a run directory and a ledger entry now exist for the
identity. That is the stale-state detector working exactly as designed, and generation 3
clears it.

```
STATE_GENERATION      3
PARENT                cdff52eea78c9763e4a04e3efc4f3d8a536305963f86fb7174e9b4eefef3b621
GEN1_UNCHANGED        YES        GEN2_UNCHANGED        YES
candidate003          DESIGNED_UNTRAINED -> TRAINED_UNEVALUATED
evaluation_corpus     null       eval-v4               FROZEN_UNUSED
```

Generation 1 and generation 2 were not touched; the archive was not touched. `PROGRESS.md`
gained current state only — no milestone report — and stays inside its size budget.

---

## 24 — Limitations

1. **Candidate 003 is trained and unevaluated.** Everything about its quality is
   **unknown, not estimated**. No gate has been evaluated and no grader has run.
2. **The validation signal is twelve rows**, it is train-side steering material, it
   appears in no gate, and it is not comparable with candidate 001's or 002's numbers.
3. **S3P is one host, one seed, one run.** No repeat, no second seed, no second host, no
   ablation. `deterministic_reproduction_claimed` is `false`.
4. **`config_hash` and `plan_hash` are root-bound.** They reproduced because this is the
   same checkout and output root; elsewhere they must be re-derived.
5. **The optimizer and scheduler actually used are transformers defaults**
   (`adamw_torch`, linear decay with the configured warmup). They are not fields of
   `TrainingConfig`, so a future upgrade could change them without moving any identity.
6. **The adapter has never been loaded for inference.** Its 392 tensors are verified as
   bytes; no forward pass has been run through the adapted model by anything except the
   trainer's own teacher-forced evaluation arm.
7. **Train/eval render parity is proven by test construction and shared enum identity**,
   not by a production cross-reference; evaluation records no render digest (S3O §9.2).
8. **`STALE_STATE_DETECTION` remains `PARTIAL`.** Gitignored artefacts are outside Git and
   cannot be diffed. The receipt closes the specific gap for *this* candidate's training
   history; it does not close the general limitation.
9. **The training corpus is synthetic and single-author** — 182 rows, one author, sharing
   a process with the held-out corpora.
10. **Semantic leakage has still never run**, and `eval-v4` remains `NOT_QUALIFIED` for it.
11. **D28, D29, D33 and D39 are all still open** and untouched, and will bound whatever a
    future evaluation can mean.
12. **The Kali training runtime is not claimed bytewise equivalent** to the Windows runtime
    that produced candidate 001's adapter.

---

## 25 — What future sessions must NOT do

- **DO NOT** create a second `TRAIN` capability for candidate 003. The attempt is spent.
- **DO NOT** retrain, resume, re-seed or "improve" this candidate. A change is a new
  identity, never a patch of this one.
- **DO NOT** evaluate it without a **separate**, explicit, single-use `EVAL` authority at
  a new generation. Reading `eval-v4` spends it permanently and obliges a fifth holdout
  for any fourth candidate (**D35**).
- **DO NOT** read `eval-v4` task bodies to explain, debug or tune candidate 003, and never
  turn a `v4` failure into a training example.
- **DO NOT** report the validation curve as a quality result or as a comparison against
  candidates 001 and 002.
- **DO NOT** rank 001, 002 and 003 in one table. Different representations, different
  exams, zero shared task instances.
- **DO NOT** paste `6f9f470f…` or `414ce9e3…` as authority on another host — re-derive.
- **DO NOT** turn D38 into a gate, widen `looks_like_refusal`, raise `max_new_tokens`, or
  fix **D39** as a rider.
- **DO NOT** promote, activate, merge, tag, release or bump the version.

---

## 26 — Exact NEXT

**An explicit HUMAN OPERATOR DECISION: whether to grant a separate, single-use `EVAL`
authority for candidate 003 against `m62-defensive-eval v4`.** S3P authorises nothing
further. It trained a candidate; it did not measure one, and it does not predict a result.

The decision is **not** implied by the candidate being trained. Spending `v4` is
irreversible: it becomes `USED_IMMUTABLE` the moment a model reads it, and a fourth
candidate would then need a fifth holdout.

If evaluation is later authorised, the only valid primary comparison is a
**simultaneously measured baseline on `v4`** versus **candidate 003 on `v4`**, under
identical generation, metric and gate policy digests — and the S3L limitations (D28, D29,
D33, the 36-task single-author holdout, lexical-only leakage evidence) must be restated in
that report.

**STOP. Wait for an explicit human operator decision. No `EVAL` authority has been
granted.**
