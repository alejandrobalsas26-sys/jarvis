# V69 M62 S3O — candidate 003: a controlled single-axis design, and the control plane that had to earn it

> **Status: `CANDIDATE_003_STATE: DESIGNED_UNTRAINED`.** A third quality candidate was
> named, configured, qualified and recorded — from body-free `eval-v4` authority only,
> changing **exactly one** primary model/training axis.
> **Zero training, zero evaluation, zero model generations, zero optimizer steps, no
> `TRAIN` or `EVAL` authority, no adapter, no `train-v3`.**

| | |
|---|---|
| Milestone | V69 M62 **S3O** — candidate-003 controlled design + Control Plane generation 2 |
| Date | 2026-08-15 |
| Branch | `jarvis-v69-m62-training-gym` |
| Starting HEAD | `d8462f58d48b230b317cfbbed810b3b3a9f490a5` |
| Master | `3705114228edef2f665be349c5c4429b7b16777a` (untouched) |
| Preceding | S3N — `V69_M62_S3N_FRESH_EVAL_V4_FREEZE.md` · S3N.1 — `V69_M62_S3N1_CONTROL_PLANE_V2_ZERO_TRUST_MIGRATION.md` |

---

## 1 — Authorisation and boundary

The operator authorised **candidate-003 controlled design + Control Plane generation 2**,
and nothing else. Measured rather than asserted:

```
TRAIN_TOKEN_CREATED / CONSUMED:        NO / NO
EVAL_TOKEN_CREATED / CONSUMED:         NO / NO
TRAIN_RUNS / EVALUATION_RUNS:          0 / 0
MODEL_WEIGHTS_LOADED:                  NO   (a TOKENIZER was loaded; see §9)
MODEL_GENERATIONS:                     0
MODEL_RESPONSE_TOKENS_GENERATED:       0
OPTIMIZER_STEPS:                       0
ADAPTER_CREATED:                       NO
TRAIN_V3_CREATED:                      NO
EVAL_PLAN_CREATED:                     NO
D37 / D38 / D39:                       FIXED / FIXED_OBSERVABILITY_ONLY / OPEN, all UNCHANGED
GATES / GRADERS / THRESHOLDS:          UNCHANGED
```

**S3O is a design milestone.** It creates the *definition* of an experiment. It does not
create the capability to run one, and it does not predict its result.

---

## 2 — Starting Git authority

Verified read-only before anything was read or written.

| | expected | measured |
|---|---|---|
| branch | `jarvis-v69-m62-training-gym` | same |
| HEAD | `d8462f58d48b230b317cfbbed810b3b3a9f490a5` | same |
| `origin/…-training-gym` | same SHA | same |
| divergence | `0 0` | `0 0` |
| `master` | `3705114228edef2f665be349c5c4429b7b16777a` | same |
| worktree | CLEAN | clean, no stash, single worktree |

---

## 3 — Control Plane verification, first

Run **before** any project reading, per the bootstrap contract:

```
python jarvis/scripts/verify_m62_control_plane.py
M62_CONTROL_PLANE_VERIFY: PASS      PROBLEMS: 0      (13/13 categories)
```

| | expected | measured |
|---|---|---|
| `SCHEMA_VERSION` | `m62.control_plane.1` | same |
| `STATE_GENERATION` | 1 | 1 |
| generation-1 snapshot | `state/m62/snapshots/0001-m62-control-plane-v2-genesis.json` | same |
| generation-1 SHA256 | `a2659d1fb1031726329394f0593478eb57b273048bc0d94faf12c89225dcf2c3` | re-hashed from bytes, matches |
| `parent_snapshot_sha256` | `null` | `null` |
| `subject_state_commit` | `ec446e348995acb0c23a69b0c3efd574f821b1a0` | same |
| archive SHA256 | `e0914054da4dde4b785bbdabc45a40e0f8b590c2aa3612e9432c685c0c79c1bf` | same |

---

## 4 — The context-reduction test — S3N.1's real exam

S3O is the first state-bearing milestone after the Control Plane V2 migration, so it is
also the migration's first real test: *can a high-impact session do its job without
opening the archive?*

```
FULL_HISTORICAL_PROGRESS_READ:   NO
HISTORY_ESCALATION_REQUIRED:     NO
```

Everything S3O needed resolved from Level 0 + Level 1 + a small set of body-free deep
authorities. The archive (516,784 bytes) was never opened — not by this session, and not
by any of the three subagents, each of which carried an explicit firewall clause naming it.

**The migration works.** Not one fact required escalation.

---

## 5 — The `eval-v4` body firewall

```
EVAL_V4_BODY_READ:   NO
HOLDOUT_FIREWALL:    PASS
```

`jarvis/scripts/build_evaluation_corpus.py` — the file holding `corpus_v4_material()` and
the authored `v4` prompts and targets — was **never opened**, by this session or by any
subagent. Neither was any task pack, promoted shard or evaluation output directory. The
verifier's own `FORBIDDEN_BODY_SOURCES` names both, and its `HOLDOUT_FIREWALL` check
passes.

What S3O used instead, all body-free and all permitted by S3N §17:

```
dataset / version       m62-defensive-eval / v4
status                  FROZEN_UNUSED
manifest                8c6871b0094bdfc75062a6352d383fa8e9750c1425182a2b3248db20500081c5
parent                  7c948236163198b5de451316e39346a37efcbc1254724f921e116a6c722f75a0
pack                    95b4e2f6ffb495735113c236f051073449f4562b780eddfc5fe8a7f76bddf2b7
tasks / splits          36 / 12-12-12
families                safety_refusal 12 · structured_report 9 · evidence_request 9 ·
                        tool_call_schema 6
decision classes        required_refusal 12 · required_completion 6 · completion 18
semantic leakage        NOT_QUALIFIED
```

Nothing in this milestone's design was chosen by looking at the exam. The axis was
**preregistered in S3N §0 before a single `v4` task existed**, and S3O implemented that
preregistration without amending it.

---

## 6 — Candidate identity: derived, not adopted

The session brief proposed `qwen3-06b-lora-quality-live-003`. That string was **not
adopted on the brief's word** — it was derived from repository convention and then found
to agree.

**Convention.** No code constructs a run id; `git grep -nE "run_id\s*=\s*f\"" -- '*.py'`
returns nothing. The authority is two hand-authored literals at
`jarvis/scripts/build_quality_training_config.py:49` and `:70`, giving the pattern:

```
<family><size>-<method>-<intent>-<mode>-<NNN>
qwen3      06b    lora     quality  live   003
```

**The ordinal is per-lineage, and this is the trap.** Three independent series exist on
this host:

| series | members present | meaning |
|---|---|---|
| `qwen3-06b-lora-smoke-live-NNN` | 001 (quarantined), 002, **003** (quarantined) | smoke/dev runs, run-004 lineage |
| `qwen3-06b-lora-quality-live-NNN` | 001, 002 | the quality candidates of record |
| `qwen3-06b-lora-live-eval-NNN` | 001 | evaluation runs |

A naive global search for `live-003` **hits** `qwen3-06b-lora-smoke-live-003` and reports
a false collision; a naive "next free ordinal" search yields a wrong `-004`. Neither is
right: the quality lineage's next ordinal is 3.

**Collision evidence:**

* no tracked path contains `quality-live-003` (`git ls-files`);
* the only tracked occurrence of the string anywhere is S3N's *negative* assertion
  `tests/test_training_gym_m62_s3n_fresh_eval_v4.py:647` — the repository had already
  written down the identity it expected not to exist yet;
* `jarvis/training_runs/runs/qwen3-06b-lora-quality-live-003` does not exist;
* the run ledger `training_runs.jsonl` holds six events over exactly three ids, none of
  them this one;
* the only ordinal-3 name in the control plane was the placeholder `candidate-003`,
  annotated in the verifier as a naming decision S3N.1 was forbidden to make.

```
CANDIDATE_003_ID:  qwen3-06b-lora-quality-live-003
EXPERIMENT_NAME:   m62-s3o-defensive-quality-003
```

S3O makes that naming decision. §12 explains why making it is not a free action.

---

## 7 — The control, reconstructed through production authority

Candidate 002 was rebuilt from the tracked generator, not read out of a document, and its
**historical identity is unchanged by S3O's edits**:

```
CANDIDATE002_CONFIG_HASH_REDERIVED:  08be37d37dd403ea8b049ab7bb32498f5d767ef013876920783ad4669e608649
                                     == the S3J/S3K sealed value
CANDIDATE001_CONFIG_HASH_REDERIVED:  e80e04e485e4405c02b0037777435986a1224a4688c9d30446991fc14555c323
                                     == the S3M.1 value for THIS root
```

Candidate 001's run record carries `b5f63cd8…` instead, and that is **not drift**: S3H
built it under the Windows root, and `config_hash` binds `output_root_id`. S3J.1 §11.1
already recorded this; S3O reproduces it rather than rediscovering it as a defect.

### 7.1 Why no config hash appears in the control plane

`TrainingConfig.config_hash()` hashes `to_dict()`, which includes `output_root_id` =
`sha256(resolved absolute output-root path)`. Measured:

```
output_root_id(.../jarvis/training_runs)      1dd79ac5ccd871741e73fee7a8af596e4fd8233145a4567e5910a21d7c62c5ac
config_hash(002) @ that root                  08be37d3…      @ any other root  2994e401…
only differing canonical key                  output_root_id
```

**A `config_hash` recorded in the snapshot would be a fact about one filesystem.** The
repository has no root-independent config identity, so S3O records none and the verifier
re-derives the design from root-independent surfaces instead (§13).

### 7.2 The control's full canonical surface

42 top-level canonical keys, of which the ones a reader will ask about:

```
base / revision      Qwen/Qwen3-0.6B @ c1899de289a04d12100db370d81485cdf75e47ca
corpus               m62-defensive-quality-train v2, manifest 24ceb1e0…, 182 records
LoRA                 r16 · alpha 32 · dropout 0.05 · bias none · CAUSAL_LM · scaling 2.0
LoRA scope           attention_and_mlp
target modules       q_proj k_proj v_proj o_proj gate_proj up_proj down_proj   (7, ordered)
LR / epochs / steps  1e-4 · 2 · 40            weight decay 0.0 · warmup 0.1
batch / grad-accum   1 × 8  (effective 8)     seed 42 · max_sequence_length 512
device / precision   cpu · fp32               dataloader_workers 0
validation           epoch (diagnostic)       checkpoints: no · max_checkpoints 1
logging              local_jsonl every 5      download policy deny
created_at_utc       2026-08-12T00:00:00Z     schema m62.training_config.1
reasoning_policy     ABSENT  ==  MODEL_DEFAULT  ==  LEGACY IMPLICIT TEMPLATE DEFAULT
```

**`optimizer`, `lr_scheduler_type`, `early_stopping` and `load_best_model_at_end` are not
config fields at all.** The backend passes `TrainingArguments` without overriding the
first two (so `adamw_torch` and linear decay with the configured warmup apply), actively
strips `EarlyStoppingCallback`, and hard-codes `load_best_model_at_end=False`. They are
*backend behaviour*, identical for both candidates and structurally incapable of becoming
a second axis. Recorded as an observation, not as a setting.

---

## 8 — The experiment, and the exact diff

Candidate 003 is built by the **same generator**, extended by the minimum that could
express it: a `"003"` identity entry, a symbolic `CANDIDATE_REASONING` mapping resolved
against the production enum, and **one keyword** in the `TrainingConfig` call.

```
CANDIDATE003_CONFIG_HASH:  6f9f470faae77b945f4ec75c3f0a25df1cbbd936d97744a9663410c53e599e1f
                           (root-bound to this host; deterministic across 3 rebuilds)
```

### 8.1 The complete raw canonical diff — four keys, nothing hidden

| key | candidate 002 | candidate 003 | class |
|---|---|---|---|
| `reasoning_policy` | **ABSENT** (= `MODEL_DEFAULT`) | `"disabled"` | **PRIMARY AXIS** |
| `run_id` | `qwen3-06b-lora-quality-live-002` | `…-003` | identity |
| `experiment_name` | `m62-s3j-defensive-quality-002` | `m62-s3o-defensive-quality-003` | identity |
| `notes` | `M62 S3J second quality candidate, option S3J …` | `M62 S3O third quality candidate, option S3J …` | provenance |

```
PRIMARY_EXPERIMENTAL_AXIS_COUNT:   1
PRIMARY_EXPERIMENTAL_AXIS_KEYS:    ["reasoning_policy"]
UNINTENDED_TRAINING_CONFIG_DIFFS:  0
```

Every other one of the 42 canonical keys is **byte-identical**, including
`dataset_reference` (all 16 sub-fields), `lora` (all 10), `resource_policy` (all 16),
`output_root_id`, `created_at_utc` and `seed`. This is asserted field by field, not in
aggregate — 39 parametrized cases in the S3O suite.

### 8.2 Why the dials cannot drift apart

Candidate 003 does **not** get a new options entry whose numbers happen to match
candidate 002's. It reuses the same key:

```python
CANDIDATE_OPTION = {"001": "B", "002": "S3J", "003": "S3J"}
```

A copied option is a thing that can be edited on one side. A shared key cannot. "Every
hyperparameter is identical" therefore reduces to one equality the verifier and the tests
both assert, rather than eight comparisons that could each rot independently.

### 8.3 What is explicitly NOT in this candidate

`ATTENTION_ONLY` · any LR, epoch, rank, alpha or dropout change · corpus rebalance ·
new structured or refusal rows · teacher-generated corpus · `train-v3` ·
`max_new_tokens` change · gate, grader, threshold or refusal-detector change · a D38 gate ·
a D39 rider fix. None of these was made.

---

## 9 — D37: the representation, qualified

### 9.1 Render identity

`chat_render_policy_hash` is `ChatRenderPolicy.render_policy_hash()`, a SHA-256 over
exactly eight string/boolean inputs — tokenizer id, tokenizer revision, chat-template
digest, reasoning policy, the library-level `enable_thinking` value,
`add_generation_prompt`, `tokenize`, policy version. No host, path, clock or environment
value can reach it. Re-derived this session:

```
CHAT_RENDER_POLICY_VERSION                    m62.chat_render_policy.1
candidate 003  prompt-prefix  DISABLED        8619f96c5ba84dab9afe19f8a0fcf385cb452680dd50374ba0e0b9a568490db0
candidate 002  prompt-prefix  MODEL_DEFAULT   892e003d29a2bbc034c0d3ee6ab4208a8bd274de21dfe24804c750a9db898a55
candidate 003  full-sequence  DISABLED        c5e83324ce311507de4c1ed5f450c7c13647dfc893e400a853f526ad12a1c6e0
TEMPLATE_DIGEST                               a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8
```

`8619f96c…` reproduces the value S3M.1 recorded. It was **re-derived, not pasted** — and
`892e003d…` ≠ `8619f96c…` is the measured statement that the axis actually moves something.

`MODEL_DEFAULT` and `DISABLED` are different *calls*, not different spellings:
`MODEL_DEFAULT.template_kwarg is None` means the keyword is **not passed at all**, and the
reviewed Jinja asks `enable_thinking is defined`. `DISABLED` passes `False`.

### 9.2 Train/eval parity is bound by object identity

```
TRAIN_EVAL_PARITY_REASONING_POLICY  is  ELIGIBILITY_REASONING_POLICY  is  ReasoningPolicy.DISABLED   →  True
```

Candidate 003 binds `TRAIN_EVAL_PARITY_REASONING_POLICY` — the shared production constant
— rather than a literal `ReasoningPolicy.DISABLED`. The two sides cannot drift apart
because they are the same object, and a test asserts that identity rather than equality.

**Honest limit.** Production evaluation does *not* construct a `ChatRenderPolicy`; it
re-implements the two-line kwarg mapping inline and records no render digest. S3M.1 made
that choice deliberately. So train/eval render parity is established **by test
construction and by shared enum identity**, not by a production cross-reference, and no
evaluation artefact carries a `chat_render_policy_hash` to compare against. That is
unchanged by S3O and is restated here so a later reader does not assume otherwise.

### 9.3 The measured effect on supervision — new, on the real corpus

S3M.1 established the mechanism on five synthetic fixtures. S3O measured it over the
**actual 154 TRAIN and 12 VALIDATION rows**, through the production encoder:

| | `MODEL_DEFAULT` (001, 002) | `DISABLED` (003) |
|---|---|---|
| rows whose **supervised** span contains `<think>` | **154 / 154** | **0 / 154** |
| full-sequence `input_ids` | *byte-identical between the two* | |
| `prompt_length` delta | — | **+4 tokens**, every row |
| supervised span | superset | exact **suffix** of the legacy span |

**What this means.** Candidates 001 and 002 were trained to emit an empty
`<think>\n\n</think>\n\n` block as the opening of every single answer. Candidate 003 will
not be. The assistant's answer text is **unchanged** by the axis — only the mask boundary
moves, reclassifying those four tokens from *target* to *prompt*.

**What this does NOT mean.** This is a representation fact, not a behavioural prediction.

```
D37_REPRESENTATION_DEFECT:        YES
D37_TERMINATION_CAUSAL_CAPABLE:   YES
D37_HISTORICAL_CAUSALITY:         NOT_ESTABLISHED
```

Nothing here says candidate 003 will fix stopping, restore 9/9 structured output, or
repair safety. **Its behaviour is unknown until it is trained and measured**, and this
document predicts no result.

---

## 10 — Tokenizer, masking and truncation

Tokenizer only, from the reviewed offline cache, `local_files_only=True`,
`trust_remote_code=False`, `HF_HUB_OFFLINE=1`. **No model weights, no optimizer, no
generation.**

```
ROWS_TESTED                       154 TRAIN + 12 VALIDATION   (both policies)
UNEXPECTED_TRUNCATIONS_AT_512     0        (both policies, both splits)
MASKING_SELF_TEST                 PASS     (production `_masking_self_test`, all rows)
check_masking PROBLEMS            0
ASSISTANT_COMPLETION_SUPERVISED   YES
TERMINATOR_SUPERVISED             YES      (`<|im_end|>` present in every label vector)
VALIDATION_CONTRIBUTES_GRADIENTS  NO       (diagnostic arm, D31; appears in no gate)
TRAIN length  min/median/max      65 / 113 / 169
VALIDATION length min/median/max  90 / 109 / 155
```

The 512 ceiling truncates nothing, with the longest row at 169 tokens — the same headroom
S3J recorded, now re-measured under both representations.

---

## 11 — Runtime, cache and plan

### 11.1 Runtime, re-checked directly rather than inferred from state

`STALE_STATE_DETECTION` is `PARTIAL`, so generation 1 cannot be trusted for runtime
readiness. The training runtime `.venv-m62-train-linux` was re-probed read-only:

```
Python 3.13.14 · torch 2.13.0+cpu · transformers 5.14.1 · peft 0.20.0 · datasets 5.0.1
trl 1.9.2 · accelerate 1.14.0 · safetensors 0.8.0 · tokenizers 0.22.2
sentencepiece 0.2.2 · numpy 2.5.2 · jsonschema 4.26.0 · huggingface_hub 1.27.0

RUNTIME_DRIFT vs S3J.1:  ZERO  (all 12 packages + interpreter)
CPU available: YES   FP32 available: YES (default dtype)   CUDA: False
build_dependency_report(TRAINING, SFT_LORA) → ready True, blockers [], missing []
```

Nothing was installed and no environment was mutated.

### 11.2 Cache

Located through the repository's own `probe_cache`, not a filesystem scan:

```
cache root              <repository parent>/.m62-model-cache   (operator-supplied,
                        outside the repository by invariant; the tests DERIVE it and
                        never write an absolute path into a tracked file)
probe_cache status      present
probe_cache evidence    f399355ef441e8ec…   == the digest S3G.1 recorded
pinned revision         c1899de289a04d12100db370d81485cdf75e47ca  present
tokenizer files         tokenizer.json · tokenizer_config.json · vocab.json · merges.txt
```

Note for a future session: the cache has **no `refs/` directory**, so a load by branch or
tag name would fail. Every M62 path pins the explicit revision sha, so this is not a
blocker — but it must stay pinned.

### 11.3 The plan — a preview, not a capability

Derived through production planning code by **two independent paths**, agreeing exactly:

| path | config origin | plan hash |
|---|---|---|
| 1 — generator | built in code by `build_config()` | `414ce9e3b8adcddbc78aa9263e2a1fc8178e83a58d14db95607f143905f2a986` |
| 2 — document round-trip | `load_training_config()` on the emitted JSON, then `plan_training()` | **identical** |

```
TRAIN_PLAN_HASH        414ce9e3b8adcddbc78aa9263e2a1fc8178e83a58d14db95607f143905f2a986
config_hash bound      6f9f470f…          plan_is_executable   True
PLAN_BLOCKERS          0
PLAN_WARNINGS          1  — "a CPU smoke run is slow; this validates the pipeline, and it
                            is not a route to a production adapter"
model cache status     present            dependency_ready     True
selected device/prec   cpu / fp32         effective batch      8
performs_training      False              creates_adapter      False
contacts_network       False              train_token_created  False
plan hyperparameters   … "reasoning_policy": "disabled" …   ← the axis is bound into the plan
```

**Path 2 deliberately avoided `train_experiment.py --print-plan`**, which legitimately
prints `confirmation_required: TRAIN:<hash>` for an operator who already holds the plan.
S3O used only the generator, which by design does not print it, and called `plan_training`
directly for the second derivation. **No `TRAIN:` string was produced anywhere in this
session.**

### 11.4 Plan hash is not authority

```
DESIGN_VALID:                      YES
PLAN_BLOCKERS:                     0
PLAN_WARNINGS:                     1
READY_FOR_TRAIN_AUTHORIZATION:     YES   (technically ready)
TRAIN_AUTHORIZED:                  NO
TRAIN_TOKEN_CREATED:               NO
```

A zero-blocker plan means a future run *could* execute. It does not mean one *may*. The
capability lives in a single-use token this milestone did not mint, plus an explicit
operator decision that has not been made. Spending requires holding a 0-blocker plan,
typing the exact `TRAIN:<hash>` into `--execute --confirm`, and the ledger accepting it
once via `consume_plan` — which was never called.

---

## 12 — Two defects found in the S3N.1 verifier, and closed

S3O is the first milestone to actually move a candidate state through Control Plane V2,
and doing so exposed two real holes.

### 12.1 A rename walked past the transition table

`check_candidate_state` keyed the transition check on `candidate_id`:

```python
previous = {c["candidate_id"]: c["status"] for c in parent["candidates"]}
before = previous.get(cid)
if before is None or before == after: continue      # ← silently skipped
```

Generation 1 recorded ordinal 3 under the **placeholder** `candidate-003`, precisely
because naming it was a decision S3N.1 was forbidden to make. So the very first legitimate
use of the table — naming the candidate — would have made `before` resolve to `None` and
skipped the check entirely. `NOT_CREATED → PROMOTED` would have passed unnoticed under a
new name.

**Fix.** The parent is resolved by `ordinal`, which is stable across a rename and was
previously schema-validated but never used semantically. A rename is permitted only when
it matches a recorded `CANDIDATE_IDENTITY_RESOLUTIONS` entry, and a candidate the parent
generation never mentioned may only *enter* as `NOT_CREATED`.

### 12.2 `DESIGNED_UNTRAINED` had no arm at all

`check_candidate_state` validated `NOT_CREATED` and `EVALUATED_*` and nothing between. A
snapshot claiming `DESIGNED_UNTRAINED` would have passed **on its own word** — exactly the
self-fulfilling control plane the zero-trust design exists to prevent.

**Fix.** Two layers, described next.

---

## 13 — The anti-circularity design

The forbidden implementation is:

> the snapshot says `DESIGNED_UNTRAINED`, a constant in the verifier says
> `DESIGNED_UNTRAINED`, they agree, therefore PASS.

That verifies nothing. So candidate 003's state is re-derived from the **production
generator**, and the snapshot must agree with *that*:

| layer | what it independently establishes |
|---|---|
| production config authority | the candidate exists, its corpus, its option, its render policy |
| training-plan authority | config/dataset/runtime/cache bindings, 0 blockers |
| deep evidence (this file) | the audited human-readable result |
| control plane | records the lifecycle state, and **grants nothing** |

`check_candidate_design` refuses a `DESIGNED_UNTRAINED` claim unless **all** of:

* a candidate in `CANDIDATES` carries that exact run id;
* its `dataset_version` matches the snapshot's `training_corpus`, and that corpus is a
  real training dataset the snapshot holds an identity for;
* the generator's base model and revision match the snapshot's;
* `candidate_reasoning_policy("003")` resolves to `DISABLED` **and is the same object**
  evaluation generates under;
* the **control** still resolves to `MODEL_DEFAULT` — if the control moves there is no
  experiment left;
* `CANDIDATE_OPTION["003"] == CANDIDATE_OPTION["002"]` — the single-axis guard;
* the `DISABLED` render identity ≠ the `MODEL_DEFAULT` one, re-derived from the
  snapshot's own template digest (which until now no check read at all);
* the deep evidence file exists **and is tracked by Git**;
* no adapter directory, no run directory, and no ledger entry names the identity.

An import failure is a **failure**, never a skipped pass.

**Deliberately not checked: `config_hash`.** It is root-dependent (§7.1). Pinning it would
pin this host, and a check that reproduces in exactly one clone is worse than no check.

---

## 14 — Control Plane generation 2

Two-phase, so the state can never describe itself.

```
PHASE A   candidate-003 design evidence: the generator extension, the S3O suite, this
          document.                                    →  subject commit
PHASE B   generation 2 snapshot, current.json, PROGRESS, history index, verifier.
```

**Between the phases the verifier deliberately FAILS `STALE_STATE`**, because
`build_quality_training_config.py` is in `STATE_BEARING_PRODUCTION` and the snapshot has
not yet moved with it. That is the detector working exactly as designed, not a defect, and
generation 2 clears it by binding the Phase-A commit as `subject_state_commit`.

```
GEN1_BYTES_UNCHANGED:   YES        GEN1_SHA_UNCHANGED:  YES
GEN2_GENERATION:        2          GEN2_PARENT:         a2659d1f… (gen 1)
```

Generation 1 was not touched. The archive was not touched. `PROGRESS.md` gained current
state only — no milestone report — and stays inside its size budget.

---

## 15 — Comparability — read this before ranking anything

**Candidate 003 is NOT directly head-to-head comparable with candidate 001 or 002.** Two
independent reasons, either sufficient:

1. **The training reasoning representation changed.** Binding the policy is itself the
   experimental axis, so 003 is fitted under a representation neither predecessor used.
2. **It will be measured on a different exam.** 001 was measured on `eval-v2`, 002 on
   `eval-v3`, and 003 will face the fresh `eval-v4` — with zero shared task instances
   between any pair.

The only valid future primary comparison is:

> a **simultaneously measured baseline** on `v4` versus **candidate 003** on `v4`, under
> identical generation, metric and gate policy digests.

Historical candidate results are **context**, not a common-test leaderboard. Any table
placing 001, 002 and 003 in one ranked column is wrong.

---

## 16 — The future evaluation contract — recorded, not created

```
EVAL_PLAN_CREATED:   NO        EVAL_TOKEN_CREATED:  NO
```

Candidate 003 has no adapter, so no honest live evaluation plan can bind an adapter hash,
an adapter manifest or a completed-run authority. Fabricating placeholders for them would
be inventing evidence, so none exist. Recorded as a **future** binding only:

```
holdout              m62-defensive-eval v4   (FROZEN_UNUSED — becomes USED_IMMUTABLE the
                                              moment it is read, and then a fourth
                                              candidate needs a fifth holdout: D35)
reasoning policy     DISABLED                max_new_tokens   512
gate policy          e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5
metric policy        e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a
generation policy    c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7
timeout_s            300, stated EXPLICITLY when the live config is built (D33: the
                     default is 120 s and the declared timeout is not enforced)
seed 11 · CPU · FP32
```

All three policy digests were re-derived from the production classes this session and are
unmoved.

**D38 stays observability-only.** No predeclared success criterion for candidate 003
mentions `output_budget_exhaustion_rate`, no gate reads it, and none may be added without a
separate operator decision. **Security remains a veto**: the refusal detector, security
grader, thresholds, training balance and holdout distribution are untouched, and candidate
003 faces the existing frozen contract. If it fails security, it fails — no quality gain
buys a security regression.

---

## 17 — Source change accounting

| kind | files |
|---|---|
| **Production (changed)** | `scripts/build_quality_training_config.py` — a `"003"` identity, `CANDIDATE_REASONING`, `candidate_reasoning_policy()`, one keyword in the `TrainingConfig` call, `CANDIDATE_OPTION["003"]`, help text |
| **Control plane (changed)** | `scripts/verify_m62_control_plane.py` — `check_candidate_design`, the `DESIGNED_UNTRAINED` arm, ordinal-keyed transitions, `CANDIDATE_IDENTITY_RESOLUTIONS`, `FROZEN_CANDIDATES` |
| **Tests (new)** | `tests/test_training_gym_m62_s3o_third_candidate.py` |
| **Tests (changed)** | `tests/test_training_gym_m62_s3j_second_candidate.py` — **one line**, §17.1 |
| **Docs / state** | this file · `PROGRESS.md` · `state/m62/…` · `jarvis/docs/m62/HISTORY_INDEX.md` |

Nothing under `training_gym/` was changed. D37, D38, D39, the gates, the graders, the
thresholds, the refusal detector and `max_new_tokens` are all untouched.

### 17.1 One pre-existing assertion retired, deliberately

`test_candidate_001_is_untouched_by_the_second_candidates_existence` asserted that
`candidate_spec("003")` raises. S3O makes candidate 003 exist, so the unknown-candidate
probe moved to `"004"`. What that line **owns** is the fail-closed property — a candidate
the generator does not name is a refusal, never a run under an authoritative-looking id —
and that property is unchanged and still asserted. It never owned the number 3. The
assertions the test exists for (candidate 001's identity, corpus and byte-exact `notes`)
are untouched, and candidate 001's `config_hash` re-derives unchanged.

This is the same shape S3N recorded: *a list moved because a version was added.*

---

## 18 — Limitations

1. **Candidate 003's behaviour is unknown, not estimated.** It has not been trained. D37's
   historical causality is `NOT_ESTABLISHED` and nothing here predicts a result.
2. **`config_hash` and `plan_hash` are root-bound.** Both values in this document are
   valid for this host's output root and must be re-derived on the executing host.
3. **Train/eval render parity is proven by test construction and shared enum identity**,
   not by a production cross-reference; evaluation records no render digest (§9.2).
4. **Two tokenizer tests skip on the authoritative interpreter.** It has no `transformers`;
   the training venv has no `pytest`, and installing either is forbidden. Their exact
   assertions were executed directly in the training runtime and passed, but no single
   interpreter on this host can collect them.
5. **Runtime absence of an adapter is host-local.** `STALE_STATE_DETECTION` remains
   `PARTIAL`: gitignored artefacts are outside Git and cannot be diffed. S3O re-qualified
   the current runtime; it did not close the general limitation.
6. **Semantic leakage is still `NOT_QUALIFIED`** for `eval-v4`, and every holdout remains
   36 synthetic tasks from one author with no independent review.
7. **D28, D29, D33 and D39 are all still open** and untouched. D29 in particular will
   bound QG-1 and SV-5 in both directions again.
8. **Measurement will remain one host, CPU, one seed, one run.**

---

## 19 — What future sessions must NOT do

- **DO NOT** train candidate 003 without a fresh, explicit, single-use `TRAIN` authority.
- **DO NOT** evaluate it without a separate single-use `EVAL` authority at a new generation.
- **DO NOT** change the axis. It was preregistered before `eval-v4` existed; swapping it
  now would make the freeze pointless.
- **DO NOT** add a second axis "while we are retraining anyway" — that is the exact
  failure this milestone exists to prevent.
- **DO NOT** read `eval-v4` task bodies to explain, debug or tune candidate 003, and never
  turn a `v4` failure into a training example.
- **DO NOT** create `train-v3`, rebalance `train-v2`, or add rows.
- **DO NOT** paste `6f9f470f…` or `414ce9e3…` as authority on another host — re-derive them.
- **DO NOT** rank 001, 002 and 003 in one table (§15).
- **DO NOT** turn D38 into a gate, widen `looks_like_refusal`, raise `max_new_tokens`, or
  fix **D39** as a rider.

---

## 20 — Final status

```
S3O_CANDIDATE003_CONTROLLED_DESIGN:    PASS
CANDIDATE_003_ID:                      qwen3-06b-lora-quality-live-003
CANDIDATE_003_PRE_STATE:               NOT_CREATED
CANDIDATE_003_POST_STATE:              DESIGNED_UNTRAINED

PRIMARY_EXPERIMENTAL_AXIS_COUNT:       1
PRIMARY_EXPERIMENTAL_AXIS_KEYS:        ["reasoning_policy"]
CONTROL_REASONING_POLICY:              MODEL_DEFAULT   (legacy implicit template default)
EXPERIMENT_REASONING_POLICY:           DISABLED
UNINTENDED_TRAINING_CONFIG_DIFFS:      0
LORA_SCOPE:                            ATTENTION_AND_MLP
TRAIN_CORPUS:                          m62-defensive-quality-train v2, unchanged
TRAIN_V2_MODIFIED / TRAIN_V3_CREATED:  NO / NO

CANDIDATE002_CONFIG_HASH_REDERIVED:    08be37d3…  (historical semantics preserved)
CANDIDATE003_CONFIG_HASH:              6f9f470f…  (root-bound, deterministic)
CHAT_RENDER_POLICY_HASH (DISABLED):    8619f96c…
TRAIN_EVAL_RENDER_PARITY:              PASS
MASKING / TERMINATOR / TRUNCATION:     PASS / SUPERVISED / 0

RUNTIME_REQUALIFIED:                   YES, zero drift
CACHE_VERIFIED:                        YES
TRAIN_PLAN_HASH:                       414ce9e3…   (two paths, identical)
PLAN_BLOCKERS / WARNINGS:              0 / 1

DESIGN_VALID:                          YES
READY_FOR_TRAIN_AUTHORIZATION:         YES
TRAIN_AUTHORIZED:                      NO
TRAIN_TOKEN_CREATED / CONSUMED:        NO / NO
EVAL_TOKEN_CREATED / CONSUMED:         NO / NO
EVAL_PLAN_CREATED:                     NO
ADAPTER_CREATED:                       NO
MODEL_WEIGHTS_LOADED:                  NO
MODEL_GENERATIONS:                     0
OPTIMIZER_STEPS:                       0

EVAL_V4_STATUS:                        FROZEN_UNUSED
EVAL_V4_BODY_READ:                     NO
FULL_HISTORICAL_PROGRESS_READ:         NO
ARCHIVE_UNCHANGED:                     YES
D37 / D38 / D39:                       FIXED / FIXED_OBSERVABILITY_ONLY / OPEN, unchanged
D38_IS_GATE:                           NO
MERGE / TAG / RELEASE / VERSION_BUMP:  NO / NO / NO / NO
```

---

## 21 — Exact NEXT

**S3O is closed and authorises nothing further.** It defined an experiment. It did not run
one, and it did not predict its outcome.

The next action is a **human operator decision** about whether to grant a separate,
explicit, single-use `TRAIN` authority for candidate 003. That decision is not implied by
`READY_FOR_TRAIN_AUTHORIZATION: YES`, which is a statement about the *pipeline*, not about
whether the experiment is worth spending a fresh holdout on.

If training is later authorised, the sequence is: a fresh plan on the executing host → a
single-use `TRAIN` token → training → `TRAINED_UNEVALUATED` at a new control-plane
generation → and only then a **separate** `EVAL` authority against `eval-v4`, which spends
the holdout permanently.

**STOP. Wait for an explicit human operator decision before any live candidate-003
training.**
