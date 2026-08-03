# V69 M62 S3B — Training execution

> **No model has been trained. No adapter exists. Nothing has been downloaded.**
> This stage ships the machinery that *would* run a supervised LoRA fine-tune when an
> operator installs the optional packages and supplies an exact plan-bound token. The
> live smoke run has **not** been performed and is **not requested**.

S3A produced a plan and a `TRAIN:<plan-hash>` token and refused to act on either. S3B
makes the token mean something: it is now spent, exactly once, to authorise exactly one
bounded run whose artifacts are checked against the plan before anything is called
complete.

---

## S3A.1 — the dataset binding, closed

S3A shipped `verify_training_dataset_reference` but nothing proved it against a real
promoted corpus. Every planner test built its reference with
`TrainingDatasetReference.placeholder()`, which short-circuits to
`INSUFFICIENT_EVIDENCE` **before a single file is opened** — so the manifest binding, the
per-split digest binding, the split-collision check, the revocation check, the export
binding and the SFT-versus-preference mismatch were all unexecuted code.

`tests/test_training_gym_m62_training_dataset_binding.py` builds the artefacts instead of
mocking the verifier:

```
TaskSpec → approved Episode/Trajectory → AggregationReport → ConsensusReport
  → hash-bound DatasetHumanReview → DatasetCandidate
  → CREATED → VALIDATED → PRIVACY_CHECKED → PROVENANCE_CHECKED → LEAKAGE_CHECKED
  → READY_FOR_PROMOTION
  → deterministic SplitPlan (plan_splits) → LeakageReport
  → PromotionPlan → PROMOTE:<hash> → immutable DatasetVersion
  → export_sft → verified SFT export manifest
  → fully specified TrainingDatasetReference → verify_training_dataset_reference
  → TrainingConfig → plan_training
```

TRAIN and VALIDATION are placed by the split hash. HIDDEN_EVALUATION and
SECURITY_REGRESSION are placed by explicit decision, because a security-regression suite
assembled by a hash function is a random sample and not a regression suite.

---

## The execution order, and why it is that order

```
1. recompute the plan from the CURRENT state of the world
2. check the token against THAT plan
3. ask the backend whether it can run this
4. mkdir(exist_ok=False)                      ← the mutual exclusion
5. consume the plan                            ← the point of no return
6. convert the corpus, import the frameworks, train
7. validate the artifacts against the plan
8. write adapter-manifest.json LAST            ← the commit point
```

Steps 1–3 cost nothing: a failure there leaves no directory, no ledger line and no
imported framework. Everything that can refuse, refuses before step 5.

### The plan is recomputed, never trusted

`run_preflight` re-derives the plan from the config, the dataset bytes, the export bytes,
the model and tokenizer identities, the dependency evidence and the measured hardware,
then checks the token against *that*. Any of these invalidates the confirmation:

dataset bytes · dataset manifest · export manifest · revocation state · model revision ·
tokenizer revision · dependency versions · selected device · selected precision ·
hardware capability category · LoRA settings · seed · resource ceilings · output-root
identity · expected artifact policy.

**What deliberately does not invalidate it:** `available_ram_gb` and
`output_disk_free_gb`. Both are raw gigabyte counts that move whenever anything else on
the host writes a file, so including them meant a token could expire between being
printed and being typed. That is not a safe failure — an operator whose token randomly
stops working learns to re-plan and paste the new one *without reading it*, and a
confirmation nobody reads is exactly the control the token exists to be. The size
*categories* stay in the digest, because they are what feasibility actually decides on.

### Replay protection

`training_runs.jsonl` lives at the **output root**, not inside `runs/<run_id>/`. Deleting
a run directory destroys the artifacts and leaves the record that the approval was spent.

The plan is consumed **before** the first irreversible act, unlike dataset promotion
which consumes after. Training is long and interruptible: consuming afterwards would mean
an interrupt at ninety percent leaves the plan replayable and a crash loop retrains
forever. The terminal outcome is appended as a **second** line; the ledger is append-only
so the `started` line is never rewritten.

Replay is keyed on the digest the **token** carries, not on the recomputed plan hash. The
plan hash covers the plan's own blockers, so a second attempt re-derives to a different
hash — the run directory now exists, which is itself a blocker — and a check keyed on the
recomputed hash would never fire.

### The state machine

```
CREATED → CONFIG_VALIDATED → DATASET_VERIFIED → DEPENDENCIES_VERIFIED
        → HARDWARE_VERIFIED → PLANNED → AWAITING_CONFIRMATION
        → PREFLIGHT_VERIFIED → STARTING → RUNNING
        → ARTIFACT_VALIDATION → COMPLETED
```

`RUNNING` has **no edge to `COMPLETED`**. A backend reporting success is a claim; the
adapter on disk is the evidence. `INTERRUPTED`, `FAILED` and `QUARANTINED` cannot reach
`COMPLETED` either. The single entrance to `RUNNING` is `STARTING`, reachable only from
`PREFLIGHT_VERIFIED`, reachable only from `AWAITING_CONFIRMATION`.

S3B never sets `EVALUATED`, `CANDIDATE` or `ACTIVE`. Those belong to S3C and the Model
Registry.

---

## Why there is no directory rename

The obvious design is to train into `.partial-<id>` and rename it into place. It was
rejected, and the reason is platform-specific and load-bearing:

- `os.replace` is atomic for a **file** within one directory. On Windows it cannot
  replace an existing **directory** at all.
- Even with a non-existent destination it fails with a sharing violation if any process
  holds a handle to any file in the tree — the normal case for a memory-mapped
  `safetensors` file, an antivirus scanner or the search indexer.
- A mechanism that works on CI Linux and fails on the operator's actual host is worse
  than no mechanism, because it fails exactly when it is load-bearing.

The commit point is a **file** instead — the pattern `write_dataset_version` already
proved. `mkdir(exist_ok=False)` is simultaneously the no-overwrite rule and the mutual
exclusion (two concurrent runs race one atomic `mkdir`; exactly one wins). Every artifact
is written atomically and re-hashed **from disk**. `adapter-manifest.json` is written
last. **A run directory without a verifiable manifest is residue, not a run.**

### Failure, interruption and quarantine

On any bad ending the partial `adapter_model.safetensors` is **deleted**, not preserved.
A truncated weights file is not diagnostic material — it is the one artifact that could
later be mistaken for a finished adapter, and the manifest that would have disproved it
is exactly the file that was never written. The directory is moved to
`quarantine/<run-id>-<nonce>/`, the terminal state is appended to the ledger, and the
plan stays consumed. A cleanup that fails is **reported**, never swallowed.

There is no automatic resume. Rerunning requires a new plan and a new confirmation.

---

## Artifacts

**Required:** `adapter_config.json`, `adapter_model.safetensors`, `adapter-manifest.json`.
**Optional:** `training_args.json`, `run.json`, `training_log.jsonl`,
`backend_result.json`, `tokenizer_config.json`, `special_tokens_map.json`.

Everything else is refused: `.bin`, pickles, scripts, shared objects, executables,
archives, symlinks, hard links, sockets, FIFOs, device files, nested directories, empty
files, oversized files. `save_pretrained(..., safe_serialization=True)` is passed
explicitly; a pickle fallback is the one output shape the policy will not accept.

The safetensors header is parsed with `json` — proving the file is structurally what it
claims and naming its tensors — rather than handed to a deserializer. Tensors that are
not LoRA parameters are refused: an adapter carrying base-model weights is a full
fine-tune in an adapter's clothing.

**The adapter digest is taken over the validated `(name, digest, size)` list**, not over
the directory. `sha256_tree` skips symlinks (so a planted link cannot pull key material
into the digest) which is also a blind spot, and — decisively — the manifest lives in the
directory it would describe, so a directory digest taken before the manifest exists can
never be re-derived after it does. A hash cannot cover itself.

`adapter-manifest.json` carries the run, plan and config hashes, the model and tokenizer
identities and revisions, the chat-template hash, every dataset digest, the seed, the
LoRA configuration, assistant-only-loss evidence, package versions, device category,
precision, step and loss metrics, and per-file digests. It carries **no** username,
hostname, home path, absolute output path, model cache location, credential, environment,
GPU serial or hidden reasoning. `completed=true` is legal only alongside
`run_state="completed"` and a positive completed-step count.

---

## Assistant-only loss

Supervised loss applies to the assistant completion only. `trl`'s
`DataCollatorForCompletionOnlyLM` is deliberately **not** used: its import location,
constructor keywords and surrounding `SFTTrainer` contract all moved across the
`trl>=0.9.6` floor this repository declares, and there is no upper bound, so a fresh
install can resolve to a version where the masking silently means something else.

Labels are built by hand with `-100`, which is
`torch.nn.CrossEntropyLoss(ignore_index=...)` — framework-level and unchanged. Every row
is checked to be a real prefix; an off-by-one mask trains on the question and reports that
it did not. A **non-vacuous** runtime self-test runs over the real tokenized rows and
fails when there are no rows, when a row has no masked prefix, or when a row has no
supervised suffix. **If masking cannot be proven, the run is blocked.**

## Tokenizer and model policy

`trust_remote_code=False` always — no config field, flag or backend option can grant it.
`local_files_only=True` unless an explicit download authorisation is active. Exact pinned
IDs and 40-character commit revisions. A missing chat template is a refusal: no generic
template is invented, because the template decides what the model actually sees.

Trainable parameters are counted and reported. Zero is refused (the run would produce the
weights it started with); more than half the model is refused (that is a full fine-tune).
`report_to=[]` and `push_to_hub=False` are set explicitly — `transformers` defaults
`report_to="all"`, which activates any tracker that merely happens to be importable.

## Model download authorisation

A download requires **all** of: the config's policy permits it, `--allow-model-download`
is on the command line, the model and tokenizer IDs and revisions are pinned and
immutable, the plan was built with the download requirement, the confirmation matches
that exact plan, and `--execute` was passed knowingly. The flag alone bypasses nothing —
a config whose policy is `deny` refuses even with the flag. There is no AUTO policy.
**No download was performed in this milestone.**

## QLoRA and DPO

`SFT_QLORA` is **planned but not executed**. Executing it needs a measured CUDA runtime,
a compatible `bitsandbytes`, a reviewed quantization config and a runtime self-test —
none of which can be written honestly without a machine to prove them on. It is left
explicitly unsupported rather than silently downgraded to ordinary LoRA, which would
produce an adapter whose manifest says `sft_qlora` and whose weights say otherwise.

`DPO_LORA` is **refused by the executor** with a typed result. The planner still describes
it. Preference-data volume and quality have not been qualified, no production DPO
evaluation gate exists, and the rejected side of every pair is material a human marked as
the *wrong* answer. `DPOTrainer` is never imported.

---

## Commands

Dry run (the default — writes nothing, downloads nothing, imports no framework):

```bash
python -m scripts.train_experiment --config training/configs/qwen3-0.6b-lora-smoke.json --json
python -m scripts.train_experiment --config <cfg> --print-plan --json
python -m scripts.train_experiment --config <cfg> --check-dependencies --json
python -m scripts.train_experiment --config <cfg> --check-dataset --json
python -m scripts.train_experiment --config <cfg> --check-hardware --json
```

Execution (requires the exact token the plan printed):

```bash
python -m scripts.train_experiment \
  --config <cfg> \
  --execute \
  --confirm TRAIN:<full-plan-hash>
```

Optional, and never sufficient on its own: `--allow-model-download`.

### Exit codes

`0` success or dry-run success · `1` refused · `2` bad invocation · `10` configuration ·
`11` dataset · `12` dependency · `13` hardware · `14` confirmation or stale plan ·
`15` replay · `16` unsupported method · `17` model access · `18` backend ·
`19` interrupted · `20` artifact validation · `21` internal.

Tracebacks are never printed by default, and no debug mode exposes credentials or private
paths.

---

## Known limitations

- **No live smoke run has been performed.** The production backend has never executed
  against a real model. It is proved structurally, not empirically.
- Bit-for-bit reproducibility is **not** claimed on accelerator hardware.
- Adapter *quality* is entirely unmeasured. This stage proves an adapter is well-formed
  and correctly bound, never that it is good.
- Dependency installation remains manual and out of scope. The installed versions are
  inside the plan hash, so installing them invalidates any outstanding confirmation and
  the plan must be reissued.
- Intermediate checkpoints default to off; automatic resume is not implemented.
- `SIGTERM` cannot be delivered on Windows by `taskkill /F` or Task Manager. Interruption
  safety therefore does not rely on a signal handler: it is structural, because the file
  that means "completed" is written last.

## What comes next

S3C — adapter evaluation against the held-out and security-regression splits — has now
shipped: see [`V69_M62_S3C_ADAPTER_EVALUATION.md`](V69_M62_S3C_ADAPTER_EVALUATION.md).
It builds the machinery that decides whether an adapter is better and still safe, and it
consumes a completed run from this stage as its input.

It changes nothing about the statements above. **No adapter has been registered,
evaluated, promoted or activated, and no model-role assignment has changed.** S3C has
never been run against a model either: its production backend is proved structurally, and
every report it can currently produce carries `empirical_status: synthetic_only`.

Model Registry promotion remains unstarted. S3C's registry bridge produces a proposal
document and writes no registry.
