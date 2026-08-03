# LoRA smoke runbook — Qwen3-0.6B

> **Status: NOT_REQUESTED.** No live smoke run has been performed. Nothing in this
> repository has downloaded a model, installed a package or trained anything. This is the
> procedure for when an operator decides to do it — a decision that is theirs, not the
> pipeline's, because it installs software, fetches weights, consumes the machine for a
> while and produces a real artifact.

---

## Before you start

Everything below happens on your machine, under your account, with your explicit
commands. Read each step's output before running the next one; the whole design assumes
you will, and the confirmation token exists precisely because you did.

You need a promoted M62 dataset version with a verified SFT export. If you do not have
one, this runbook is not your next step — the dataset pipeline is
(`docs/V69_M62_S2D_*`).

---

## 1. Plan first. Change nothing.

```bash
cd jarvis
python -m scripts.train_experiment \
  --config training/configs/qwen3-0.6b-lora-smoke.json \
  --dataset-root <your dataset store> \
  --json
```

This writes nothing, downloads nothing and imports no training framework. Read the
`blockers` and `missing_evidence` lists. Expect, on a machine that has never done this:

- `dependency: torch is not installed` and friends → step 2;
- `Qwen/Qwen3-0.6B: the revision is still the template placeholder` → step 3;
- the dataset reference is still `REQUIRED_*` placeholders → step 4.

## 2. Install the optional training profile — yourself

The pipeline never installs anything. Run it deliberately, and look at what it pulls in:

```bash
python -m pip install -r requirements/training.txt
```

CUDA users additionally: `python -m pip install -r requirements/training-cuda.txt`.
This is a large download (torch alone is on the order of a gigabyte). It is unrelated to
the model weights, which come later and separately.

> The installed **versions are inside the plan hash.** Installing or upgrading anything
> here invalidates any confirmation token you already have. Always re-plan afterwards.

## 3. Pin an immutable model revision

A tag is not immutable and is refused. Find the exact 40-character commit sha of the
snapshot you intend to train against on the model's hub page, and put it in the config's
`base_model_revision` **and** `tokenizer_revision` — they must be identical, because the
same repository at two revisions is two different vocabularies.

## 4. Fill in the dataset reference

Every `REQUIRED_*` slot must become a real 64-character digest, re-derived from your
promoted version. `--check-dataset` tells you which are still unfilled and which do not
match the bytes on disk.

## 5. Cache the weights, or authorise a download

Cache-only is the default and the safe path. If you already have the model in a hub
cache, point at it:

```bash
python -m scripts.train_experiment --config <cfg> --check-hardware \
  --model-cache-root <your hub cache> --json
```

If you do not, a download is required, and it needs **all** of: the config's
`model_download_policy` set to `allow_with_explicit_flag`, the `--allow-model-download`
flag, pinned immutable revisions, and a confirmation matching the plan that was built
with the download requirement. The flag on its own bypasses nothing.

## 6. Plan again, and read it

```bash
python -m scripts.train_experiment --config <cfg> --print-plan --json
```

`is_executable` must be `true` and `blockers` must be empty. The document ends with:

```
"confirmation_required": "TRAIN:<64-character plan hash>"
```

**Read the plan before you copy that token.** It names the exact dataset digests, the
exact model revision, the device, the precision, the seed and every hyperparameter. The
token authorises that state of the world and nothing else — if anything changes, it stops
matching, which is the point.

## 7. Run it

```bash
python -m scripts.train_experiment \
  --config <cfg> \
  --dataset-root <your dataset store> \
  --output-root <your training tree> \
  --execute \
  --confirm TRAIN:<full-plan-hash>
```

The plan is spent exactly once, whatever the outcome. A failure, an interruption and a
success all consume it; rerunning requires a new plan and a new token. Deleting the run
directory does not give it back — the ledger lives at the output root, not inside the run.

Expect a small model on CPU to take minutes to hours depending on your machine. Ctrl+C is
safe: the run becomes `INTERRUPTED`, the partial weights file is deleted, the directory is
moved to `quarantine/`, and no adapter manifest is ever written.

## 8. Check what you got

A completed run leaves `<output-root>/runs/<run_id>/` containing
`adapter_config.json`, `adapter_model.safetensors`, `adapter-manifest.json` and the
bounded logs. The manifest is the commit point: **a run directory without one is residue,
not a run.**

```bash
python - <<'PY'
from training_gym.training.artifacts import verify_completed_run
from training_gym.training.config import load_training_config
cfg = load_training_config("<cfg>")
print(verify_completed_run("<output-root>/runs/<run_id>",
                           lora=cfg.lora.to_dict(),
                           base_model_id=cfg.base_model_id) or "verified")
PY
```

---

## What this does not tell you

The adapter is **well-formed and correctly bound**. That is all that has been checked.
Nothing here measures whether it is any good — evaluation against the held-out and
security-regression splits is S3C, and it has not been built. Do not promote, register,
activate or serve an adapter on the strength of a completed run.

## Troubleshooting

| Exit | Meaning | Usual cause |
|---|---|---|
| 11 | dataset | the corpus or export changed after you planned; re-plan |
| 12 | dependency | the optional profile is not installed, or a version is below floor |
| 13 | hardware | the requested device or precision is not measurably available |
| 14 | confirmation | something changed since the token was issued; re-plan and read the new plan |
| 15 | replay | this token already started a run; a new plan is required |
| 16 | unsupported | DPO, or QLoRA, which are planned but not executed |
| 17 | model access | weights are not cached and no download authorisation is active |
| 19 | interrupted | Ctrl+C or a cancellation; the run is in `quarantine/` |
| 20 | artifact | the backend "succeeded" but what it wrote is not an acceptable adapter |

A `14` immediately after a `12` is normal and correct: installing packages changed the
dependency evidence, which changed the plan, which invalidated the token.
