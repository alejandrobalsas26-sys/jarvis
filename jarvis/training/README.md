# `training/` — V65 M17 reproducible training experiments

This tree holds the **inputs and metadata** of training experiments driven by
`core/training_pipeline.py`. Training itself is **never** run on the chat event
loop, never automatic, and never faked: an experiment plans and validates here,
and only an explicit, out-of-loop launcher (with an available backend) ever
produces a real adapter.

## V69 M62 S3A — the planner and the launcher

`python -m scripts.train_experiment` now exists. It is the module
`core/training_pipeline.py` has named in its generated argv since V65 M17, and it is a
**planner**: it validates a strict `training_gym.training.config.TrainingConfig`, verifies
the M62 dataset version it names, reports which packages and which hardware are actually
present, produces a deterministic `TrainingPlan`, and prints the `TRAIN:<plan-hash>` token
that would authorise that plan and no other.

It does not train. `--execute` refuses and exits nonzero; the training backend ships in
M62 S3B. Nothing here downloads a model, installs a package, contacts a network or
creates an adapter. See `docs/V69_M62_S3A_TRAINING_PLANNER.md`.

```bash
python -m scripts.train_experiment \
  --config training/configs/qwen3-0.6b-lora-smoke.json --dry-run
```

`configs/qwen3-0.6b-lora-smoke.json` is a reviewed **template** — the model revision and
every dataset digest are `REQUIRED_*` placeholders, so it parses, says exactly what is
unfilled, and reports `INSUFFICIENT_EVIDENCE` until they are replaced. It is the one JSON
in `configs/` that is tracked; per-run configs remain gitignored.

```
training/
  configs/     TrainingConfig JSON written by TrainingPipeline.write_config (run inputs)
               plus the tracked S3A sample template (qwen3-0.6b-lora-smoke.json)
  datasets/    imported/pinned dataset versions (M16 output dirs; provenance-checked)
  runs/        run.json metadata per run_id (status, plan, gates, artifact) — append-only
  adapters/    materialized adapter artifacts from completed external runs
  logs/        training logs from the external launcher
  manifests/   dataset/import manifests
```

## Contract (enforced by `core/training_pipeline.py`)

- An experiment may consume **only** an M16-produced dataset (or an import with an
  equivalent `manifest.json`). `verify_dataset` re-checks existence, manifest,
  pinned version, **content-hash match**, all-`APPROVED` status, no quarantined/
  rejected/secret-bearing records, schema, and a minimum sample count.
- A **dry run** reports estimated examples/tokens, sequence length, memory
  pressure, backend availability, and the expected artifact path — without
  executing.
- Backends generate **argv lists** (`shell=False`), never shell strings.
- Execution is explicit (`execute(config, confirm=run_id)`) and requires an
  **available** backend. On the current local host (torch + transformers present;
  `peft`/`trl`/`bitsandbytes` absent) LoRA/QLoRA/DPO are *planned but not
  executable* — the pipeline records an honest `FAILED` run, never a fake success.
- A `run_id` can never silently overwrite another (`save_run` fail-closed).

Generated `runs/`, `adapters/`, and `logs/` content is git-ignored (see
`.gitignore`); the directory structure and this README are tracked.

---

## V69 M62 S3B — the training gym's own execution path

`core/training_pipeline.py` (above) remains the M16/M17 authority and is unchanged. The
M62 training gym has its own, stricter path under `training_gym/training/`, and this
directory is where it writes.

| Directory / file | Written by | Tracked? |
|---|---|---|
| `runs/<run_id>/` | a completed M62 run | no |
| `quarantine/<run_id>-<nonce>/` | a failed or interrupted M62 run | no |
| `training_runs.jsonl` | the plan-consumption ledger | no |
| `configs/qwen3-0.6b-lora-smoke.json` | reviewed template, `REQUIRED_*` placeholders only | **yes** |

`.gitignore` here is an **allowlist of names**, so every new generated artifact must be
added deliberately. A run ledger records exactly which dataset digests a model was fitted
on; it is reviewed locally and never committed.

**A run directory without `adapter-manifest.json` is residue, not a run.** The manifest is
written last, after every other file has been re-hashed from disk, so no partial
`adapter_model.safetensors` is ever readable as a finished adapter.

- Contract and threat model: [`docs/V69_M62_S3B_TRAINING_EXECUTION.md`](../docs/V69_M62_S3B_TRAINING_EXECUTION.md)
- Planner: [`docs/V69_M62_S3A_TRAINING_PLANNER.md`](../docs/V69_M62_S3A_TRAINING_PLANNER.md)
- Step-by-step first run: [`docs/LORA_SMOKE_RUNBOOK.md`](../docs/LORA_SMOKE_RUNBOOK.md)

**No live smoke run has been performed.** No model has been downloaded and no adapter
exists in this repository. `SFT_QLORA` is planned but not executed; `DPO_LORA` is refused
by the executor. Adapter evaluation and Model Registry promotion are S3C and are not
started.

## V69 M62 S3C — adapter evaluation

`python -m scripts.evaluate_adapter` compares a pinned base model against the same base
model plus a verified adapter. It is dry-run by default and creates nothing; `--execute`
refuses on every host today and names which precondition is missing.

An adapter produced by a run under `runs/` is the *input* to that comparison. Its outputs
live under `evaluation/`, not here. See `docs/V69_M62_S3C_ADAPTER_EVALUATION.md` and
`docs/ADAPTER_EVALUATION_RUNBOOK.md`.

**No adapter has been evaluated.** The production evaluation backend has never been run
against a model, and every report this build can produce carries
`empirical_status: synthetic_only`.
