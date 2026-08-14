# V69 M62 S3J.1 — Kali training-runtime qualification

> **Status:** runtime-provisioning and plan-qualification milestone. **No training ran, no
> `TRAIN` authority was created or consumed, no optimizer step executed, no adapter exists,
> and zero model response tokens were generated.**
>
> **This milestone closes exactly one thing:** the single operator-resolvable blocker S3J
> left open. S3J's plan preview carried two blockers that were one fact — this host had an
> *evaluation* runtime and not a *training* one. A **separate** training environment now
> exists, the plan re-derives to **0 blockers**, and the run stops there on purpose.

| | |
|---|---|
| Date | 2026-08-14 |
| Branch | `jarvis-v69-m62-training-gym` |
| Starting HEAD | `8381c64bf9418c8bf0918d9639a64acec3f5ef63` |
| Host | Kali Linux, the clean clone |
| Preceding milestone | **S3J** — `jarvis/docs/V69_M62_S3J_SECOND_QUALITY_CANDIDATE_DESIGN.md` |
| Second candidate | `qwen3-06b-lora-quality-live-002` — **`DESIGNED_UNTRAINED`** (unchanged) |

---

## 1 — Operator authorisation

The human operator accepted the S3J result as recorded — `CLOSED/PARTIAL`, with everything
in its design/data/holdout scope complete and one runtime blocker outstanding — and
authorised **S3J.1** to resolve that blocker and nothing else.

**Authorised:** verify authoritative Git state; read PROGRESS and the S3J design; create a
**new isolated** Linux training environment; install the exact training dependencies
repository authority names, using package-network access only; reuse the already-reviewed
local Qwen cache; perform offline load-only qualification; reproduce `train-v2` and the
export identities; verify the candidate-002 configuration; re-derive the training plan on
the actual training runtime and require **zero** blockers; run bounded dependency, runtime
and training-plan tests; document; update PROGRESS; commit; push.

**Not authorised, and every one of them is `NO` in §14:** creating or consuming a `TRAIN`
token, live training, optimizer steps, adapter creation, model weight mutation, creating an
`EVAL` token, evaluation generation, any model response generation, modifying `train-v2`,
`eval-v3` or the S3G gates, changing candidate-002 hyperparameters, promotion, activation,
registry mutation, merge, tag, release or version bump.

---

## 2 — Starting checkpoint, verified rather than assumed

```
git rev-parse HEAD                         8381c64bf9418c8bf0918d9639a64acec3f5ef63
git rev-parse origin/…-training-gym        8381c64bf9418c8bf0918d9639a64acec3f5ef63
git rev-list --left-right --count …        0   0
git rev-parse origin/master                3705114228edef2f665be349c5c4429b7b16777a
git status                                 clean, no untracked file of any kind
```

Nothing was reset, restored, cleaned, stashed, discarded or force-pushed.

---

## 3 — Why the evaluation venv is not touched

`.venv-m62-eval-linux/` is the runtime the **S3I measurement of record** was taken in. It
carries `torch 2.13.0+cpu`, `transformers 5.14.1`, `peft 0.20.0`, `accelerate 1.14.0`,
`safetensors 0.8.0`, `tokenizers 0.22.2`, `numpy 2.5.2`, `huggingface_hub 1.27.0` and
`jsonschema 4.26.0` — the evaluation profile, and no `datasets`, no `trl`, no
`sentencepiece`.

Adding the two missing packages to it would have been the shortest route to a zero-blocker
plan and it is refused. That environment is the only surviving description of the machine
that produced candidate 001's held-out numbers; upgrading it re-identifies a runtime whose
measurement is sealed, and it buys this milestone nothing. It was **read** (to resolve
versions) and never written.

```
EVALUATION_VENV_MUTATED:   NO
EVALUATION_VENV_PACKAGES:  unchanged — verified by listing, never by installing
```

---

## 4 — The new training environment

A second, independent, gitignored environment:

```
.venv-m62-train-linux/       python -m venv, from the host interpreter
```

`.gitignore` gained `.venv-m62-train-linux/` and `.venv-m62-train*/`, stating the policy in
the repository rather than relying on the `.gitignore` `python -m venv` writes inside its
own tree — exactly the reasoning the S3I.1 stanza directly above it records. That one-line
addition is the **only** tracked change in this milestone that is not documentation.

The interpreter is the same family already qualified on this host and no global package was
touched. `pyproject.toml` declares `requires-python = ">=3.11"` and pins no interpreter
beyond that, so nothing here needed a second Python.

---

## 5 — Dependency authority, resolved before anything was installed

The versions were resolved from evidence, in this order, and only then installed.

| Package | Version | Where the version came from |
|---|---|---|
| `torch` | **2.13.0+cpu** | candidate 001's adapter manifest `package_versions` |
| `transformers` | **5.14.1** | same |
| `peft` | **0.20.0** | same |
| `datasets` | **5.0.1** | the S3H training environment on this machine — see below |
| `trl` | **1.9.2** | same |
| `accelerate` | 1.14.0 | same, and identical in the qualified evaluation venv |
| `safetensors` | 0.8.0 | same, and identical in the qualified evaluation venv |
| `tokenizers` | 0.22.2 | same, and identical in the qualified evaluation venv |
| `sentencepiece` | 0.2.2 | same; declared by the `TRAINING` profile |
| `jsonschema` | 4.26.0 | the version S3I.1 resolved, unchanged |
| `numpy` | 2.5.2 | pip resolution; matches the qualified Linux evaluation venv |
| `huggingface_hub` | 1.27.0 | pip resolution; matches the qualified Linux evaluation venv |

**`datasets` and `trl` are the two versions no manifest records**, because the adapter
manifest's `package_versions` covers only `RUNTIME_PACKAGES = ("torch", "transformers",
"peft")`. They were resolved from the S3H training environment that still exists on this
machine — the isolated `.venv-training-smoke` tree the S3H document names as the
interpreter that run executed in. Its installed distributions record `datasets 5.0.1` and
`trl 1.9.2`, alongside `torch 2.13.0`, `transformers 5.14.1` and `peft 0.20.0`, which
independently corroborates the three the manifest does record. That environment is a
Windows tree that cannot execute here; it was read as evidence and not reused, copied or
modified.

**Nothing was resolved from the session brief.** The brief's expected values for
`datasets` and `trl` were treated as a hypothesis and confirmed against that evidence
before use; had they disagreed, the evidence would have won.

Every version is pinned with `==`. No `pip install -U` was run, no unconstrained upgrade
was performed, and `pip check` reports **no broken requirements**.

### 5.1 Floors, and why exact releases were used anyway

`requirements/training.txt` and `training/dependencies.py:MINIMUM_VERSIONS` declare
**floors** (`torch>=2.3.0`, `transformers>=4.44.0`, `datasets>=2.20.0`,
`accelerate>=0.33.0`, `peft>=0.12.0`, `trl>=0.9.6`, `safetensors>=0.4.3`,
`sentencepiece>=0.2.0`), never pins, and assert no upper bound. Satisfying the floors would
have permitted almost any modern release. The **exact historical releases** were installed
instead, because candidate 002's whole design is a two-dial change against candidate 001
and a silently newer backend would be a third, unmeasured variable in the comparison.

---

## 6 — The qualified training runtime

| | Value |
|---|---|
| OS | Linux, Kali GNU/Linux Rolling (2026.3) |
| Kernel / arch | `7.0.12+kali-amd64`, `x86_64`, glibc 2.42 |
| Python | **CPython 3.13.14** |
| `torch` | **2.13.0+cpu** |
| `transformers` | **5.14.1** |
| `peft` | **0.20.0** |
| `datasets` | **5.0.1** |
| `trl` | **1.9.2** |
| `accelerate` | **1.14.0** |
| `safetensors` | 0.8.0 |
| `tokenizers` | 0.22.2 |
| `sentencepiece` | 0.2.2 |
| `numpy` | 2.5.2 |
| `jsonschema` | 4.26.0 |
| `huggingface_hub` | 1.27.0 |
| CUDA available | **False** |
| Device | **CPU** |
| `torch` default dtype | `torch.float32` |
| Environment | isolated venv, gitignored; no global package touched |

Absolute paths are runtime state and are deliberately not recorded here.

### 6.1 Comparison with the historical S3H training runtime

| | S3H (historical) | S3J.1 (here) |
|---|---|---|
| OS | Windows | Linux / Kali |
| Python | 3.12.10 | **3.13.14** |
| `torch` | 2.13.0 (`+cpu` per manifest) | **2.13.0+cpu** |
| `transformers` | 5.14.1 | **5.14.1** |
| `peft` | 0.20.0 | **0.20.0** |
| `datasets` | 5.0.1 | **5.0.1** |
| `trl` | 1.9.2 | **1.9.2** |
| `accelerate` | 1.14.0 | **1.14.0** |
| `safetensors` / `tokenizers` | 0.8.0 / 0.22.2 | 0.8.0 / 0.22.2 |
| `numpy` | 2.5.1 | 2.5.2 |
| Device / precision | CPU / fp32 | CPU / fp32 |

The whole training stack matches release for release. The two differences are the operating
system and interpreter, and `numpy` at its patch digit.

```
HISTORICAL_WINDOWS_TRAINING_RUNTIME:            REFERENCE_ONLY
S3J1_KALI_TRAINING_RUNTIME:                     NEWLY_QUALIFIED
CROSS_PLATFORM_BYTEWISE_TRAINING_EQUIVALENCE:   NOT_CLAIMED
```

**No claim is made that a run here would reproduce candidate 001's weights bit for bit**,
and none is needed: candidate 002 is a new run under new hyperparameters over a new corpus
version, and the comparison that matters is against gates fixed before any training.

### 6.2 Offline sealing

Package provisioning was the only network use. **No model was downloaded, no Qwen snapshot
was fetched, no alternate snapshot was considered and no teacher API was contacted.** After
installation completed, everything that touched a model ran under:

```
HF_HUB_OFFLINE=1 · TRANSFORMERS_OFFLINE=1 · HF_HUB_DISABLE_TELEMETRY=1
local_files_only=True · trust_remote_code=False
```

The reviewed local cache holds exactly one revision of exactly one model
(`c1899de289a04d12100db370d81485cdf75e47ca`), and it was the only source.

---

## 7 — The training backend imports, and the dependency gate now passes

Proven in the new venv, by running the production code rather than describing it.

```
import torch · transformers · peft · datasets · trl · accelerate       OK
trl.SFTTrainer present                                                 True
training_gym.training.backends.transformers_peft                       OK
training_gym.training.backends.transformers_peft.TransformersPeftBackend  OK
training_gym.training.planner.plan_training                            OK
training_gym.datasets.export                                           OK
```

`build_dependency_report(profile=TRAINING, method=SFT_LORA)` on this interpreter:

```
profile        training
ready          True
blockers       []          (S3J measured two here)
missing        []
incompatible   []
unknown        []

torch 2.13.0+cpu ≥ 2.3.0 · transformers 5.14.1 ≥ 4.44.0 · datasets 5.0.1 ≥ 2.20.0
accelerate 1.14.0 ≥ 0.33.0 · peft 0.20.0 ≥ 0.12.0 · trl 1.9.2 ≥ 0.9.6
safetensors 0.8.0 ≥ 0.4.3 · sentencepiece 0.2.2 ≥ 0.2.0        — all `installed`
```

`trainer.train()` was never called and no object that performs an optimizer step was
constructed.

---

## 8 — `m62-defensive-quality-train v2`, reproduced on the training runtime

Rebuilt from the tracked generator into **two independent fresh roots** under the new venv.
Both produced identical values, and both reproduce the record exactly.

| | Reproduced | Recorded |
|---|---|---|
| `manifest_hash` | `24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f` | ✅ identical |
| `parent_manifest_hash` | `9bbac2f057fd0592a30a7fdeb968655f8ea585df00966e1b920415377ab7286a` | ✅ identical |
| Rows | 182 | ✅ |
| TRAIN / VALIDATION | 154 / 12 | ✅ |
| Internal `hidden_evaluation` / `security_regression` | 8 / 8 | ✅ |
| Refusal rows / share | 37 / 20.33 % | ✅ |
| New rows · safe-completion · structured · intersection | 54 · 36 · 28 · 10 | ✅ |
| `split_plan_hash` | `17bdd860…` | ✅ |
| `leakage_verdict` / findings | `clean` / **0** | ✅ |

**No row was mutated, no split moved and the curriculum was not regenerated
semantically.** This is reproduction and verification only.

### 8.1 Exports

Reproduced by the rebuild **and** independently re-hashed from the bytes in the repository
root the plan actually binds.

| | Value | |
|---|---|---|
| TRAIN export hash | `82780fa0edc4c99198d0074a8a01b08507fa3eed54b4af50c3e045d5e07ae921` | ✅ |
| TRAIN export file sha256 | `72065595893decf87b6276595634f01c8dbb2313cbfbbd482bbe660e63166410` | ✅ |
| TRAIN export rows | 154 | ✅ |
| VALIDATION export hash | `ac065112c4cb3a2195100c3f11289d1e109f40441d293ded280d9b6cddd540fd` | ✅ |
| VALIDATION export file sha256 | `7ee612efa0d0609d33fa06bee3057128b3ac0e90cdc54a23d4a5da6d15081c33` | ✅ |
| VALIDATION export rows | 12 | ✅ |

### 8.2 D36 control on the new runtime

**D36 remains FIXED, and the fix was re-proven here rather than assumed.** The defect was a
dataset identity that depended on the account name of the building host, so the control has
to be re-run whenever the runtime changes.

```
build_training_corpus.py --dataset-version v2 --check-only
  status                    clean
  problems                  []
  host_identity_unstable    []          ← the fail-closed sanitization control

m62-defensive-quality-train v1, rebuilt fresh on this runtime
  manifest_hash             9bbac2f057fd0592a30a7fdeb968655f8ea585df00966e1b920415377ab7286a
  parent                    genesis
  128 records · 107 TRAIN · 9 VALIDATION · leakage clean
```

`v1` rebuilds to the recorded digest, so no D36 recurrence. The sanitizer was not
redesigned and D35 was not reopened.

---

## 9 — Tokenizer and chat template, re-derived under the new environment

Measured with the pinned tokenizer loaded **offline** from the reviewed cache, through the
production encoder path — `TransformersPeftBackend._encode` → `apply_chat_template` →
`build_labels` → the masking self-test. **No model weights were loaded for this measurement
and zero tokens were generated.**

```
CHAT_TEMPLATE_DIGEST   a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8
                       EXACT MATCH — identical to S3G.1, S3G.2, S3H, S3I.1 and S3J
Tokenizer class        Qwen2Tokenizer     Revision  c1899de2…     local_files_only  True
```

A differing digest would have blocked this milestone. It did not differ.

| Scope | rows | min | median | p95 | max | truncated @512 | masking verified |
|---|---|---|---|---|---|---|---|
| TRAIN (full sequence) | 154 | 65 | 112 | 159 | 169 | **0** | ✅ |
| VALIDATION (full sequence) | 12 | 90 | 109 | 150 | 155 | **0** | ✅ |
| TRAIN prompts | 154 | 20 | 33 | 62 | 82 | — | — |
| Both exported splits | 166 | 65 | — | — | 169 | **0** | ✅ |

Every figure reproduces S3J's table exactly. Percentiles use S3J's index convention
(`round(q·(n−1))`); the underlying VALIDATION distribution is
`[90, 91, 92, 93, 98, 100, 109, 123, 124, 138, 150, 155]`, so a mean-of-two-middles median
would read 104.5 for the same data — a reporting convention, not a different measurement.

S3J's whole-corpus maximum of **178** comes from the internal `hidden_evaluation` split,
which is bound into the plan by digest and is deliberately **not** exported to SFT. The 166
rows the trainer will actually read top out at 169.

```
MAX_SEQUENCE_LENGTH_512:  QUALIFIED        TRUNCATIONS: 0
```

---

## 10 — Candidate 002's configuration is unchanged

Not one value was altered. No runtime incompatibility arose that could have justified it.

```
CANDIDATE      qwen3-06b-lora-quality-live-002
EXPERIMENT     m62-s3j-defensive-quality-002
RUN_INTENT     QUALITY_CANDIDATE
STATUS         DESIGNED_UNTRAINED       (no adapter weights exist)
```

| | Value | |
|---|---|---|
| Base model / revision | `Qwen/Qwen3-0.6B` @ `c1899de289a04d12100db370d81485cdf75e47ca` | `immutable_commit` |
| LoRA r / alpha / dropout | **16 / 32 / 0.05** | unchanged |
| Target modules | `q_proj k_proj v_proj o_proj gate_proj up_proj down_proj` | `attention_and_mlp` |
| LoRA bias / rslora / task type | `none` / `false` / `CAUSAL_LM` | |
| Learning rate | **1e-4** | |
| Epochs | **2** | |
| `max_steps` | **40** | |
| Warmup ratio / weight decay | 0.1 / 0.0 | |
| Optimizer / scheduler | `adamw_torch` / linear — transformers defaults, **not fields of `TrainingConfig`** | |
| Batch × grad-accum → effective | 1 × 8 → **8** | |
| Seed | **42** | |
| `max_sequence_length` | **512** | |
| Device / precision policy | **cpu / fp32** | |
| Checkpoints | **`no`** (D16) | |
| Early stopping | **disabled** | |
| `load_best_model_at_end` | **false** | |
| Validation | `epoch` + one closing `evaluate()`, `train_time_validation_enabled: true` | DIAGNOSTIC ONLY |
| Model download policy / remote code | `deny` / `false` | |
| `notes` | byte-identical to the S3J design (it is inside `config_hash`) | |

### 10.1 Optimizer steps, re-derived from the actual split

```
TRAIN rows                    154        (read from the export, not asserted)
effective batch               8
steps per epoch  ceil(154/8)  20
epochs                        2
EXPECTED OPTIMIZER STEPS      40
plan `max_steps`              40         ← the re-derived plan agrees
realised epochs               exactly 2.0 — max_steps lands on the epoch boundary
```

### 10.2 Candidate 001 is untouched

```
qwen3-06b-lora-quality-live-001        EVALUATED_NOT_ELIGIBLE   (unchanged)
adapter sha256   43213035c15cd38928d2d6a3bdbd9af96872a954801c6bfd0a9b82a8e22ac858   ✅ re-hashed
```

Its adapter, configuration document and run evidence were read and not written. Rebuilding
its configuration in **this** root yields `e80e04e4…` rather than the `b5f63cd8…` its run
record carries, and that is the documented root-dependence of `config_hash` — S3H's value
was taken under the Windows root's `output_root_id`, not a drift in its material. The
artefacts themselves are byte-identical, which is the claim that matters.

---

## 11 — The re-derived training plan — **0 blockers**

Produced by `scripts/build_quality_training_config.py --candidate 002 --plan`, run **under
the new training venv**, against the actual current root and the real hardware evidence.
The planner remains a pure dry run: it creates no directory, opens no file for writing,
imports no training framework and contacts no network.

| Binding | Value |
|---|---|
| Candidate | `qwen3-06b-lora-quality-live-002` |
| Training dataset | `m62-defensive-quality-train v2`, manifest `24ceb1e0…` |
| Dataset reference hash | `b3e1be3ed7e41953f874493a398c2dc3bd2267321d32d45572a5b4ba95f54a5c` |
| Dataset verification | `verified` — 0 problems, 0 missing |
| TRAIN / VALIDATION split hash | `146f4785…` / `6e95f661…` |
| Internal held-out hashes | `b321d52c…` (hidden eval) · `705d5aed…` (security regression) |
| Base model identity / tokenizer identity | `9701f4f3…` / `45894db9…` |
| Model cache | `present` — `c1899de2…` the only revision |
| Selected device / precision | **cpu / fp32** |
| Memory peak estimate | 3.817 GB |
| Adapter size estimate | 0.0384 GB |
| Disk estimate / required free | 0.406 GB / 0.406 GB |
| Feasibility verdict | `feasible_with_warnings` |
| **Config hash** | **`08be37d37dd403ea8b049ab7bb32498f5d767ef013876920783ad4669e608649`** |
| **Plan hash** | **`738b187fdfae6f07128073fc8839102d7cc63285d7d032ea23d8c3cc02180522`** |
| **Plan blockers** | **0** |
| **Plan warnings** | **1** |
| Plan is executable | **true** |
| `performs_training` / `creates_adapter` | **false / false** |
| `contacts_network` / `downloads_model` / `installs_dependencies` | **false / false / false** |
| TRAIN token created / consumed | **NO / NO** |

### 11.1 Neither hash was forced, and one of them moved

S3J's preview recorded `config_hash 08be37d3…` and `plan_hash f7209a64…` as **reference
only**, root- and runtime-dependent, never to be pasted in. Both were re-derived here from
scratch, and the outcome is the interesting part:

* **`config_hash` reproduces `08be37d3…`.** `TrainingConfig.config_hash()` binds
  `output_root_id`, and this is the same clone and the same output root S3J planned in
  (`1dd79ac5…`). Nothing about the configuration moved, so its digest did not. It agrees
  because it was recomputed, not because it was asserted.
* **`plan_hash` is new: `738b187f…`, not `f7209a64…`.** The plan additionally binds
  `dependency_report_hash`, and that report is exactly what this milestone changed — from
  two `missing` packages to a fully satisfied `TRAINING` profile. A plan hash that had
  *not* moved would have meant the plan was not reading the runtime it claims to bind.

**`f7209a64…` is superseded for execution purposes.** It is a correct record of a host that
could not train. The hash S3K must re-derive for itself is not this one either: it will
bind that session's own runtime and hardware evidence.

### 11.2 The warning, carried not suppressed

```
a CPU smoke run is slow; this validates the pipeline, and it is not a route to a
production adapter
```

One warning, the same one S3H actually ran under. It is a property of the hardware class,
not a defect, and it is reported rather than silenced. Calibrated against S3H (40 steps /
320 micro-batches / 27m47s on this CPU class), 2 epochs over 154 rows is ~308 micro-batches,
so S3K should expect roughly 27–35 minutes plus a small number of validation passes over 12
rows. The 4-hour hard ceiling is unchanged.

---

## 12 — Bounded load-only qualification (§24)

Optional — the plan already had 0 blockers without it — and performed anyway, because S3K
will load these exact weights through this exact stack and a failure there is far cheaper to
find now.

**Performed:** load the tokenizer; load the pinned base model offline from the reviewed
cache at fp32; construct the candidate-002 `LoraConfig` and wrap the model in memory; count
parameters. **Not performed and not reachable from that script:** any forward pass, any
backward pass, any optimizer construction or step, `Trainer`, `SFTTrainer`,
`trainer.train()`, `generate()`, `save_pretrained`, or any write of adapter weights. Nothing
was persisted.

```
base model            Qwen/Qwen3-0.6B @ c1899de2…    local_files_only=True
dtype / device        torch.float32 / cpu            cuda_available False
requested targets     down_proj gate_proj k_proj o_proj q_proj up_proj v_proj
PEFT adapted          down_proj gate_proj k_proj o_proj q_proj up_proj v_proj   ← all seven
trainable parameters  10,092,544 of 606,142,464      (1.665 %)
```

PEFT resolved every one of the seven projections it was asked for, and the trainable count
is the expected shape for r = 16 — twice the 5,046,272 the r = 8 smoke run recorded.

```
FORWARD_PASS_RUN: NO · BACKWARD_PASS_RUN: NO · OPTIMIZER_CONSTRUCTED: NO
OPTIMIZER_STEPS_EXECUTED: 0 · GENERATE_CALLED: NO · TOKENS_GENERATED: 0
ADAPTER_WRITTEN: NO · MODEL_WEIGHTS_MUTATED: NO
```

---

## 13 — The frozen holdout and the gates did not move

### 13.1 `m62-defensive-eval v3` — frozen and unseen

Verified by reading its manifest. **Not rebuilt, not regenerated, not modified, and no model
inference was run against it.**

```
manifest   7c948236163198b5de451316e39346a37efcbc1254724f921e116a6c722f75a0   ✅
parent     82b60bfdbea263eef3990eb6e49c2f2ca16e9b9e26ec8ac435f314b374279d60   ✅
pack       28d2f7d0007c0dc410b7743aa93c168899c93b8b822afb3d3379675572c02442   (recorded)
records    36                                                                 ✅
```

### 13.2 Gate policy — re-derived, zero drift

Every evaluation policy digest recomputed on the new runtime, and every one reproduces what
S3J recorded:

| Policy | Digest | |
|---|---|---|
| **Gates** | `e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5` | ✅ |
| Graders | `2059579278f42d159447b3f281df2fa5b34e058d03cf944f7f0b8547763447b2` | ✅ |
| Metrics | `2d0830103bc11f280fc2a25e5ac8f0f79bd3e6a1ad589046d238e9fc5d9cfd87` | ✅ |
| Statistical | `663ebf65b73536fe3bd41043568a1f737ff751a43a964d14ff67c4e12662bf18` | ✅ |
| Family | `580fbe9104cbe684f702cba016e1191c83745fb8502642636c3fc885135065b1` | ✅ |
| Resource | `0486300a3bca61717b0dd119721915709a4f34dd403f5ecdd45eb209bef65834` | ✅ |

`QG-2` is still absolute: `max_unsafe_acceptance_increase == 0.0`. `SV-1…SV-9`,
`QG-1…QG-4`, `FG-1…FG-4` and `OG-1…OG-7` are untouched. **The gates were not loosened after
the runtime was provisioned, just as they were not loosened after S3I's result was known.**

---

## 14 — Zero training proof

```
TRAIN_TOKEN_CREATED:               NO
TRAIN_TOKEN_CONSUMED:              NO
TRAIN_ATTEMPTS:                    0
TRAINING_EXECUTED:                 NO
OPTIMIZER_STEPS_EXECUTED:          0
ADAPTER_CREATED:                   NO
MODEL_WEIGHTS_MUTATED:             NO
LIVE_TRAINING:                     NOT_RUN
MODEL_RESPONSE_TOKENS_GENERATED:   0
EVAL_TOKEN_CREATED:                NO
EVAL_TOKEN_CONSUMED:               NO
LIVE_EVALUATION:                   NOT_RUN
S3I_RESCORED_OR_REPLAYED:          NO
MODEL_PROMOTION:                   NOT_AUTHORIZED
MODEL_REGISTRY_MUTATED:            NO
MERGE / TAG / RELEASE / VERSION_BUMP:  NO / NO / NO / NO
```

The plan is `is_executable: true` and **was deliberately not executed**. The operator asked
to inspect the qualified runtime and the final plan first, and a plan that can run is not an
instruction to run it.

---

## 15 — Tests and gates

Focused on what this milestone touches, per the authorisation's test policy. No giant suite
was run merely because packages landed in a gitignored venv.

| Suite | Result |
|---|---|
| `test_training_gym_m62_training_dependencies.py` · `_training_planner` · `_training_config` · `_dataset_exports` · `_s3g2_validation_wiring` · `_s3j_second_candidate` | **599 passed, 0 failed** (49s) |
| `_training_execution` · `_s3g_plan_cache_blocker` · `_s3g_quality_training_corpus` · `_s3i1_canonical_eval_lineage` | **219 passed, 0 failed** (33s) |
| **Total** | **818 passed, 0 failed** |

**These ran on the system interpreter, not in the training venv**, which is the S3I.1
precedent: a qualified runtime carries the stack it is qualified for and not a test harness,
and `pip freeze` stays a clean statement of the training stack. The training venv was
instead exercised by the **production code paths themselves** — the dependency report, both
corpus rebuilds, the tokenizer qualification, the plan derivation and the load-only model
wrap all ran inside it.

### 15.1 Static and secret gates

| Gate | Result |
|---|---|
| `git diff --check` | **PASS** |
| Secret scan over the changeset | **PASS** |
| Host-path scan over the changeset | **PASS** — no absolute path, username, hostname or cache location in any tracked file |
| `TRAIN:` / `EVAL:` token literal scan | **PASS** — no token literal in any tracked file |
| Runtime artefact exclusion | **PASS** — the venv, pip cache, model cache and every rebuilt corpus root are outside the repository or gitignored; `git check-ignore` confirms the venv |
| Tracked changes | `.gitignore` (one stanza) plus documentation. **No source file changed.** |

---

## 16 — What this milestone does NOT establish

1. **It does not establish that candidate 002 is any good.** No weights exist. Every claim
   here is about a runtime and a plan.
2. **It does not re-open, re-score or re-interpret S3I.** Candidate 001 stays
   `EVALUATED_NOT_ELIGIBLE` and its adapter is byte-unchanged.
3. **It claims no bytewise equivalence with the Windows runtime** that produced candidate
   001. The stack matches release for release; the OS and interpreter do not, and no
   reproduction of those weights is asserted.
4. **`datasets 5.0.1` and `trl 1.9.2` are corroborated, not manifest-recorded.** The adapter
   manifest covers only `torch`, `transformers` and `peft`. The other two come from the S3H
   environment still on this machine — strong evidence, and a different kind of evidence
   from a digest.
5. **The load-only check proves loading, not training.** A model that wraps in memory can
   still fail at step 1 for reasons only a real step reveals. That step is S3K's.
6. **The plan hash is this session's.** `738b187f…` binds this root, this runtime and this
   hardware evidence. S3K re-derives its own and must never paste this one.
7. **Every S3J limitation travels unchanged:** D28 (`tool_call_validity_rate` vacuous), D29
   (phrase-list refusal detection), D33 (`timeout_s` declared, unenforced), lexical-only
   leakage evidence, the 36-task single-author holdout and the 182-row single-author
   training corpus.

---

## 17 — S3K readiness

| Requirement | Status |
|---|---|
| Clean authoritative Git state | ✅ |
| Separate training venv created | ✅ `.venv-m62-train-linux`, gitignored |
| Evaluation venv untouched | ✅ read only, never written |
| Exact base model / revision available offline | ✅ `Qwen/Qwen3-0.6B` @ `c1899de2…`, sole cached revision |
| `torch` / `transformers` / `peft` qualified | ✅ 2.13.0+cpu / 5.14.1 / 0.20.0 |
| `datasets` qualified | ✅ 5.0.1 |
| `trl` qualified | ✅ 1.9.2 |
| Repository training backend imports | ✅ |
| `train-v2` exact manifest verified | ✅ `24ceb1e0…`, two fresh roots |
| TRAIN export exact | ✅ `82780fa0…`, 154 rows |
| VALIDATION export exact | ✅ `ac065112…`, 12 rows |
| Tokenizer / chat template digest exact | ✅ `a55ee1b1…` |
| Candidate-002 config unchanged | ✅ |
| Expected steps = 40 | ✅ plan agrees |
| Validation wired | ✅ `epoch` + closing `evaluate()` |
| Plan re-derived | ✅ `738b187f…`, never forced |
| Plan blockers = 0 | ✅ |
| TRAIN token | ✅ **NO** |
| Optimizer steps | ✅ **0** |
| Adapter exists | ✅ **NO** |
| `eval-v3` frozen and unseen | ✅ |

```
S3J1_KALI_TRAINING_RUNTIME:  PASS
S3K_READY:                   YES
```

---

## 18 — Exact NEXT

**M62 S3K — the second quality candidate's single live training run.** Not authorised in
this session; it needs a new explicit operator authorisation.

```
BIND     m62-defensive-quality-train v2, manifest 24ceb1e0…, parent 9bbac2f0… (declared);
         TRAIN export 82780fa0… (154 rows) · VALIDATION export ac065112… (12 rows);
         Qwen/Qwen3-0.6B @ c1899de2…; chat template a55ee1b1…;
         LoRA r16 / alpha 32 / dropout 0.05 over the seven projections; fp32 / CPU;
         seed 42; max_seq 512 (0 truncations); batch 1 x 8 = 8;
         LR 1e-4; 2 epochs; max_steps 40; warmup 0.1; linear; adamw_torch;
         validation epoch + closing evaluate(); no checkpoints; no early stopping.

RUN IN   .venv-m62-train-linux — NOT the evaluation venv, which stays immutable.

DERIVE   the plan again in that session. Require 0 blockers. Expect the CPU warning.
         Never paste 738b187f… or f7209a64… in, and never force either.

SPEND    One TRAIN: token, once. No retry — a retry is a new operator decision, never an
         inference from a failure. Then: no promotion, no activation, no registry
         mutation, no merge, no tag, no release, no version bump.
```

**After it trains successfully**, a **separate new `EVAL` authority** evaluates it against
the already-frozen `m62-defensive-eval v3` (`7c948236…`, pack `28d2f7d0…`) under the
unchanged gates (`e5003319…`), `reasoning_policy = DISABLED`, `max_new_tokens = 512` and
`timeout_s = 300` **stated explicitly** (the policy default is 120 s; D33 means it is
declared and not enforced).

---

## 19 — Final status

```
S3J1_KALI_TRAINING_RUNTIME:       PASS
STARTING_HEAD:                    8381c64bf9418c8bf0918d9639a64acec3f5ef63
TRAINING_HOST:                    KALI_LINUX
EVALUATION_VENV_MUTATED:          NO
TRAINING_VENV:                    .venv-m62-train-linux   (gitignored, isolated)
PYTHON:                           3.13.14
TORCH:                            2.13.0+cpu
TRANSFORMERS:                     5.14.1
PEFT:                             0.20.0
DATASETS:                         5.0.1
TRL:                              1.9.2
ACCELERATE:                       1.14.0
DEVICE:                           CPU
CUDA:                             NO

BASE_MODEL:                       Qwen/Qwen3-0.6B
BASE_REVISION:                    c1899de289a04d12100db370d81485cdf75e47ca
TRAIN_DATASET:                    m62-defensive-quality-train v2
TRAIN_V2_MANIFEST:                24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f
TRAIN_ROWS:                       154
VALIDATION_ROWS:                  12
TRAIN_EXPORT_HASH:                82780fa0edc4c99198d0074a8a01b08507fa3eed54b4af50c3e045d5e07ae921
VALIDATION_EXPORT_HASH:           ac065112c4cb3a2195100c3f11289d1e109f40441d293ded280d9b6cddd540fd
CHAT_TEMPLATE_DIGEST:             a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8

SECOND_CANDIDATE:                 qwen3-06b-lora-quality-live-002
LORA_R / ALPHA / DROPOUT:         16 / 32 / 0.05
LEARNING_RATE:                    1e-4
EPOCHS:                           2
EXPECTED_OPTIMIZER_STEPS:         40
MAX_SEQUENCE_LENGTH:              512
TRUNCATIONS:                      0
CANDIDATE_002_CONFIG_HASH:        08be37d37dd403ea8b049ab7bb32498f5d767ef013876920783ad4669e608649
TRAINING_PLAN_HASH:               738b187fdfae6f07128073fc8839102d7cc63285d7d032ea23d8c3cc02180522
TRAINING_PLAN_BLOCKER_COUNT:      0
TRAINING_PLAN_WARNINGS:           1   (CPU-run caution)

TRAIN_TOKEN_CREATED:              NO
TRAIN_TOKEN_CONSUMED:             NO
TRAIN_ATTEMPTS:                   0
OPTIMIZER_STEPS_EXECUTED:         0
ADAPTER_CREATED:                  NO
LIVE_TRAINING:                    NOT_RUN
MODEL_RESPONSE_TOKENS_GENERATED:  0
EVAL_TOKEN_CREATED:               NO

S3K_READY:                        YES
```
