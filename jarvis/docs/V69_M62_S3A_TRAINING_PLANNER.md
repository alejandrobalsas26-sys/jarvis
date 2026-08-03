# V69 M62 S3A — The Training Planner

**Status:** implemented
**Scope:** planning only. This stage does not train.

---

## What S3A is

The complete contract and dry run for a LoRA fine-tuning experiment:

- a strict, closed **training configuration** schema;
- a pinned **model and tokenizer identity**;
- a **dataset reference** verified against the M62 dataset authorities;
- a passive **dependency report**;
- a passive **hardware capability report**;
- deterministic **memory, disk and feasibility** estimates;
- an immutable **TrainingPlan** with a digest that would authorise exactly one
  future execution;
- the `scripts/train_experiment` launcher, which plans and refuses to execute.

It fills a gap that had been open since V65 M17: `core.training_pipeline`'s
`TransformersPeftBackend.generate_command` has been emitting the argv
`python -m scripts.train_experiment --config ... --method ... --output ...`
for several milestones, and the module it named did not exist.

## What S3A is not

It does **not** train. There is no trainer, no optimizer, no adapter writer and no
checkpoint writer in this stage.

It has never been run against Qwen3-0.6B or any other model. No model has been
downloaded, no tokenizer has been instantiated, no adapter exists, no adapter has
been evaluated, no model has been promoted, and no model-role default has changed.
QLoRA and DPO are *planned* here and have never been *executed* anywhere in this
repository. There is no claim of Ollama compatibility and no claim of production
readiness.

`--execute` does not fail — it **refuses**, because there is no branch in this stage
that leads to a training loop. The execution backend ships in **S3B** and is now
implemented — see [`V69_M62_S3B_TRAINING_EXECUTION.md`](V69_M62_S3B_TRAINING_EXECUTION.md).
This document describes the planning stage, which is unchanged: it still writes
nothing, downloads nothing and imports no training framework. What changed is that
the `TRAIN:<plan-hash>` token it prints is now spendable, exactly once, by
`--execute`. No live smoke run has been performed.

---

## Why the dry run performs no training

A planner is only useful if it is safe to run. Tests assert, after a full plan:

| Property | How it is asserted |
|---|---|
| No file or directory is created | directory listing compared before and after |
| No socket is opened, no name resolved | `socket.connect`/`create_connection`/`getaddrinfo` monkeypatched to raise |
| No process is spawned | `subprocess.run`/`Popen`/`os.system` monkeypatched to raise |
| No pip / uv / poetry / conda / winget / apt / sudo | asserted over the **AST** of every S3A module — the identifiers are absent from executable code |
| No training framework is imported | `sys.modules` delta measured **after** the plan, so a lazy in-function import is caught |
| No adapter or checkpoint is created | `rglob("*.safetensors")`, `rglob("adapter_*")` are empty |
| No absolute path, username or hostname is emitted | the output is searched for `tmp_path`, `~` and `Users` |

Package availability is answered by `importlib.util.find_spec` and
`importlib.metadata.version`. Neither loads the package. A planner advertised as pure
that imports the training stack to discover whether the training stack is there is not
pure.

## Why the dependencies are optional

`requirements/training.txt` and `requirements/training-cuda.txt` are opt-in profiles.
They are absent from `base` — so `pip install jarvis` never pulls a torch build — and,
unlike every other optional profile, they are **absent from `all` as well**:
`pip install jarvis[all]` is the full workstation for someone who wants every
capability, not a multi-gigabyte download for someone who never asked to fine-tune
anything.

`bitsandbytes` is split into `training-cuda` because outside a CUDA environment it
either fails to install or installs and then fails at import; in the generic profile it
would make that profile uninstallable on the CPU host this repository is developed on.

**JARVIS never installs these.** The planner reports what is present and refuses to
plan an executable run without it. It does not call pip. `install_hint()` returns prose
for a human to run; there is no code path that runs it.

Install them yourself, when you have decided to:

```bash
pip install -r requirements/base.txt -r requirements/training.txt
# CUDA hosts only, and still not sufficient for QLoRA on its own:
pip install -r requirements/base.txt -r requirements/training-cuda.txt
```

## Why model download is denied

`ModelDownloadPolicy` has exactly two members, `DENY` (the default) and
`ALLOW_WITH_EXPLICIT_FLAG`. There is deliberately no `AUTO`: a policy that decides for
itself whether to reach the network is a policy that reaches the network.

S3A downloads nothing under **any** policy, any flag, and any combination of the two.
`--allow-model-download` changes only what the report says a *future* execution would
require. Cache presence is inspected by looking for a directory *name* under a root the
operator supplied explicitly; the status and a digest leave the module, the path never
does, and an unreadable root reports `UNKNOWN` rather than `ABSENT` — because a probe
that could not run has not shown that weights are missing.

---

## Config schema

`training_gym/training/config.py`, schema `m62.training_config.1`. Unknown fields fail
closed.

The security invariants are **unrepresentable**, not denylisted. There is no field for
an executable, a Python module path, extra argv, an environment variable, an absolute
output path, an API token, a cache directory or a telemetry endpoint. `trust_remote_code`
exists as a field for exactly one reason: so a config that asks for it is refused loudly
rather than silently reinterpreted. It may only ever be `false`, and no flag overrides
that.

| Group | Fields |
|---|---|
| Identity | `schema_version`, `run_id`, `experiment_name`, `method`, `created_at_utc` |
| Model | `base_model_id`, `base_model_revision`, `base_model_parameters_b`, `base_model_family`, `tokenizer_id`, `tokenizer_revision`, `trust_remote_code` |
| Data | `dataset_reference`, `training_split`, `validation_split`, `hidden_evaluation_reference`, `security_regression_reference` |
| Optimization | `seed`, `epochs`, `max_steps`, `batch_size`, `gradient_accumulation_steps`, `learning_rate`, `weight_decay`, `warmup_ratio`, `max_sequence_length`, `gradient_checkpointing` |
| Execution shape | `precision_policy`, `device_policy`, `lora`, `dataloader_workers` |
| Bookkeeping | `checkpoint_strategy`, `checkpoint_interval_steps`, `max_checkpoints`, `logging_target`, `logging_interval_steps` |
| Governance | `output_root_id`, `model_download_policy`, `dependency_profile`, `resource_policy`, `notes` |

Refused, among others: unknown fields, duplicate JSON keys, bare `NaN`/`Infinity`,
symlinked or hard-linked config files, files over 256 KiB, unsafe `run_id`
(traversal, separators, Windows device names, trailing dots), absolute or UNC
`output_root_id`, a tokenizer from a different repository or at a different revision,
`trust_remote_code: true`, a held-out split named as the training split, and any
hyperparameter outside its declared ceiling.

`TrainingResourcePolicy` holds every ceiling. **No member may be zero** — a ceiling of
zero is not "unlimited", and this policy has no way to express unlimited. Its version
and its hash are inside the plan hash, so a plan approved under one set of limits cannot
be executed under a laxer one.

### Training methods

`SFT_LORA`, `SFT_QLORA`, `DPO_LORA`. Full fine-tuning is absent; so is online
reinforcement learning and continuous autonomous training. An unknown method fails
closed.

- **SFT_LORA** — may produce a valid plan. Execution disabled until S3B.
- **SFT_QLORA** — planned only with CUDA *and* bitsandbytes evidence; reported
  unsupported otherwise. Execution disabled.
- **DPO_LORA** — planned only against a verified **preference** export; an SFT export
  is refused, because an SFT file has no rejected side. Not production-ready.

### LoRA

Bounded rank (≤256), alpha (≤1024), dropout in `[0, 1)`, a closed target-module policy
(`all_linear`, `attention_only`, `attention_and_mlp`) — never a caller-supplied regex,
never remote module discovery, never an empty target set. Every value is in the plan
hash, and a record may not disagree with its own derived `target_modules` or `scaling`.

The smoke defaults are rank 8, alpha 16, dropout 0.05, bias none, `all_linear`. That is
recorded as **a tested starting configuration**, not as a claim that `all-linear` is
universally optimal.

---

## DatasetReference verification

`training_gym/training/dataset_reference.py`. This defines no dataset — a third
definition would be the point at which a bridge quietly becomes a second writer. It
defines a *reference* and verifies it through the existing authorities.

A caller-supplied record count is a claim; a caller-supplied digest is a claim.
Verification converts claims into evidence by loading the artifact and re-deriving the
value:

1. ids validated **before** any `Path` join;
2. `datasets.manifests.load_manifest` — the only sanctioned loader, which re-hashes every
   shard from disk and refuses symlinks and multiply-linked files, before it returns;
3. the manifest digest must equal the one the reference names;
4. every required split's manifest-record digest and shard-bytes digest must match;
5. train and validation may not be empty;
6. no two required splits may share a shard digest;
7. no revoked candidate may sit in a train-side split;
8. no evaluation-only or dataset-ineligible record may sit in a train-side split;
9. the version must record a seed, a split-policy hash and a leakage-report hash;
10. the SFT/preference export is verified through `verify_sft_export` /
    `verify_preference_export`, and its manifest digest and row digest must match;
11. a preference method against an SFT export — and an SFT method against a preference
    export — are both refused;
12. the record count must equal the verified total.

`REQUIRED_*` placeholders are deliberately invalid digests. A template **parses**, so the
planner can report exactly what is unfilled, and **can never verify**, so it can never
become executable. No plausible-looking fake digest appears anywhere.

---

## Model and tokenizer identity

A revision is executable only as a full 40-character **lowercase** commit sha. A branch
name is well-formed and reported as *not reproducible* — the same argument
`sandbox/security.validate_image` already makes about container tags, applied to the
other thing this system pins. The revision is not lower-cased before matching: an
uppercase string is a different string, and accepting it would make a plan hash depend on
how the operator typed it.

The tokenizer must be the base model's own repository at the same revision. A substituted
tokenizer changes what every token id means and raises no error anywhere, so it is
refused rather than warned about.

---

## Hardware capability report

Cheap facts (OS family, Python version, architecture, logical CPUs, RAM, free disk at the
configured output root) come from `sys`, `platform`, `os` and `psutil`.

Whether CUDA *works* is not a cheap fact, so **no accelerator probe runs implicitly**.
`HardwareCapabilityReport.detect()` performs one only when handed an explicit probe
callable; the default reports `UNKNOWN`. A probe that raised reports `UNKNOWN` too — a
probe that crashed measured nothing, and `UNAVAILABLE` would be a claim it cannot make.

**Package presence is never read as capability.** `find_spec("torch") is not None` means
a wheel is installed; it does not mean a GPU exists, a driver matches, or bitsandbytes
will import. Those are separate fields and neither substitutes for the other.

Never recorded: hostname, username, GPU UUID, serial numbers, MAC addresses, absolute
paths, or the environment block. RAM and disk are a rounded whole number of gigabytes
plus a coarse category.

### Device policy

`AUTO_SAFE` (default), `CPU`, `CUDA`, `MPS`, `DIRECTML`.

`AUTO_SAFE` selects only from measured capability and records the reason. An explicit
`CUDA` request on a host with no CUDA evidence is a **refusal**, never a CPU run: falling
back silently turns a thirty-minute job into a twelve-hour one without telling anyone.
`DIRECTML` is recognised as an inference path on this repository's Windows hosts and is
refused for training — it is not an established Transformers/PEFT training backend and
this milestone will not claim otherwise on its behalf.

### Precision policy

`AUTO_SAFE`, `FP32`, `FP16`, `BF16`, `INT8_TRAINING`, `INT4_QLORA`.

Unsupported precision is refused, never downgraded: a run that quietly becomes fp32 when
bf16 was asked for produces different numbers, a different memory profile and a different
wall clock from the one that was approved. fp16 on a CPU is refused. bf16 without
measured support is refused. `INT4_QLORA` needs CUDA **and** bitsandbytes. `INT8_TRAINING`
is refused for want of evidence — quantized *inference* support is a different claim from
quantized *training* support.

---

## Estimates are estimates

`training_gym/training/feasibility.py` produces a component breakdown — base weights,
gradients, optimizer state, activations, adapter parameters, quantization overhead,
dataloader, safety margin — and every estimate carries its **formula version**, its
method, its precision, its parameter class and its **assumptions**. A number without
those is a claim the reader cannot check and the author cannot reproduce.

It also cross-checks against `core.training_pipeline.estimate_memory_gb` and **warns when
the two disagree by more than a factor of two**, so the repository has one authority per
stage rather than two silently-diverging numbers. The S3A breakdown governs S3A planning;
the M17 scalar remains what the M17 `DryRunPlan` reports.

An estimate is not a measurement. A 25% safety margin is inside every peak, and a comfort
margin is not a PASS.

**Verdicts:** `FEASIBLE`, `FEASIBLE_WITH_WARNINGS`, `INSUFFICIENT_EVIDENCE`,
`UNSUPPORTED`, `BLOCKED`.

**Runtime is a category, never a duration:** `SHORT_SMOKE`, `SLOW_LOCAL_SMOKE`,
`IMPRACTICAL_LOCAL`, `REQUIRES_ACCELERATOR`, `UNKNOWN`. CPU Qwen3-0.6B is
`SLOW_LOCAL_SMOKE` — it is not fast. Anything at or above 3B parameters on a CPU is
`REQUIRES_ACCELERATOR`; Qwen3-8B is not a practical local run on this host.

---

## Plan hashing and the future confirmation

`TrainingPlan.plan_hash()` is `sha256` over the canonical plan, which **excludes the hash
itself**, the effects block and the confirmation token. It covers the config hash, both
identity hashes, every dataset digest, the dataset-verification digest, the dependency
report hash, the hardware report hash, the feasibility report hash, the selected device
and precision, every hyperparameter, the LoRA configuration, the resource-policy version
and hash, and the planner and schema versions.

The digest is not a signature over an intention. It is a signature over a **state of the
world**: a plan approved on Tuesday cannot execute on Wednesday against a dataset that was
edited on Wednesday morning.

The absolute output root is never published — only `sha256(resolve().as_posix())`, so two
roots produce two plans and neither plan carries the operator's home directory into a
ticket.

### The token, for S3B

```
TRAIN:<64 lowercase hex characters>
```

Validated by `check_training_confirmation`, which refuses: a `bool` (by type, before any
string handling — `confirm=True` is the shape of every accidental automation), any
non-string, anything containing `@`, `/` or `\` (a confirmation read from a file is one
nobody typed), a wrong prefix, a digest that is not exactly 64 characters, and any token
whose plan is not the plan recomputed from current inputs. Comparison is
`hmac.compare_digest`. There is no `--force`, no `--yes`, no generic confirmation, no
environment-variable source and no file source.

**S3A never consumes a token.** It validates the syntax and reports it, because there is
no execution to authorise.

---

## Commands

```bash
# The default: a dry run. Plans, verifies, reports; changes nothing.
python -m scripts.train_experiment \
  --config training/configs/qwen3-0.6b-lora-smoke.json \
  --dry-run

# Machine-readable, for a ticket or a CI step.
python -m scripts.train_experiment \
  --config training/configs/qwen3-0.6b-lora-smoke.json \
  --dry-run --json

# Which training packages are installed? Imports none of them; installs nothing.
python -m scripts.train_experiment \
  --config training/configs/qwen3-0.6b-lora-smoke.json \
  --check-dependencies

# What can this host actually do? Runs no accelerator probe.
python -m scripts.train_experiment \
  --config training/configs/qwen3-0.6b-lora-smoke.json \
  --check-hardware

# Verify the dataset version and export the config names.
python -m scripts.train_experiment \
  --config training/configs/qwen3-0.6b-lora-smoke.json \
  --check-dataset --dataset-root <store>

# Just the plan record and the token that would authorise it.
python -m scripts.train_experiment \
  --config training/configs/qwen3-0.6b-lora-smoke.json \
  --print-plan --json
```

`--execute` is **refused** in this stage. It is documented only to show the refusal:

```bash
$ python -m scripts.train_experiment --config <cfg> --execute --json
{
  "ok": false,
  "execution_backend": "not_implemented_until_s3b",
  "error": "training: execution is not implemented in this stage. ... M62 S3A
            contains no trainer, no optimizer and no adapter writer ...
            Nothing was started, nothing was downloaded and no output was created.",
  "confirmation_consumed": false,
  "trained_anything": false,
  "created_adapter": false,
  "downloaded_anything": false
}
$ echo $?
1
```

Exit codes: `0` success, `1` refused or verification failed, `2` bad invocation.

---

## The sample configuration

`training/configs/qwen3-0.6b-lora-smoke.json` — `Qwen/Qwen3-0.6B`, SFT LoRA, 1 epoch,
batch 1, gradient accumulation 8, sequence length 512, learning rate 2e-4, LoRA rank 8 /
alpha 16 / dropout 0.05, seed 42, `max_steps` 50.

It exists to **validate the pipeline**. It is not a route to a production adapter, and a
CPU run of it would be slow.

It is a template: the model revision and every dataset digest are `REQUIRED_*`
placeholders rather than plausible-looking fake values. It parses, reports exactly what is
unfilled, and returns `INSUFFICIENT_EVIDENCE` until the placeholders are replaced with
verified values. No commit revision was invented, and nothing was browsed or downloaded to
discover one.

Generated `runs/`, `adapters/`, `logs/` and per-run `configs/*.json` stay gitignored; this
one reviewed template is the single tracked exception. `training_gym/` and `scripts/` are
not in `[tool.setuptools].packages`, so none of this ships in `pip install jarvis`.

---

## Known limitations

- **No execution.** S3A has never trained anything. Every capability statement here is
  about planning.
- **Dataset verification against real M62 artifacts is exercised only through the
  placeholder path in this milestone's tests.** The verification code calls the M62
  authorities (`load_manifest`, `verify_sft_export`, `verify_preference_export`) which
  have their own S2d suites, but end-to-end tests that build a promoted dataset version
  and then verify a fully-specified reference against it are **not yet written**. Treat
  the fully-specified path as implemented and not yet independently covered.
- **Dependency version floors are lower bounds only.** No upper bound was verified,
  because verifying one would have required resolving packages over the network. They are
  a bounded requirement, not a tested compatibility range.
- **The LoRA trainable-parameter count is a proportional model**, not a derivation from a
  specific architecture — this module cannot see one. It is stated as an assumption on
  every estimate.
- **Activations are modelled linearly** in batch × sequence length; attention is quadratic
  in sequence length. Stated in the assumptions.
- **DirectML is not classified as a training backend**, and MPS support has not been
  exercised on Apple hardware from this repository.
- The M17 `DryRunPlan.output_artifact_path` still contains an absolute path. S3A's own
  records do not, but the older field was left as-is rather than changed underneath its
  existing callers.

## The S3B boundary

S3B owns everything S3A refuses:

- the training backend itself (Transformers/PEFT/TRL);
- consuming a `TRAIN:<plan-hash>` token, once, against a replay ledger
  (`training_runs.jsonl`);
- recomputing the plan from current inputs before acting on it;
- honest `RUNNING` / `INTERRUPTED` / `FAILED` / `COMPLETED` states — a run killed at
  step 400 must not leave a partial `adapter_model.safetensors` that reads as complete;
- adapter identity and digest (`sha256_tree`, which skips symlinks, so a link inside an
  adapter directory is invisible to the digest — stated in the plan's effects);
- model download under `ALLOW_WITH_EXPLICIT_FLAG`, if the operator authorises it.

Evaluation, promotion and any change to model-role defaults remain gated and manual,
after S3B.
