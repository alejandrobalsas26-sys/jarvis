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
