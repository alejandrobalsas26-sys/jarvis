# V69 M62 — First verified live LoRA smoke

**Date (UTC):** 2026-08-05
**Status:** PASS
**Scope:** pipeline validation only.

This records the first live SFT-LoRA run that produced a real adapter and survived
structural verification. It says nothing about whether the adapter is any good — see
[What this does not establish](#what-this-does-not-establish).

## Run

| | |
|---|---|
| Method | `SFT_LORA` |
| Base model | `Qwen/Qwen3-0.6B` |
| Base model revision | `c1899de289a04d12100db370d81485cdf75e47ca` (immutable commit) |
| Tokenizer | `Qwen/Qwen3-0.6B` @ `c1899de289a04d12100db370d81485cdf75e47ca` |
| Device | CPU |
| Precision | FP32 |
| Requested steps | 4 |
| Completed steps | 4 |
| Epochs completed | 4.0 |
| Train loss | 3.283784 (finite) |
| Eval loss | not measured — no evaluation arm runs at this stage |
| Duration category | minutes (slow local smoke) |
| Interrupted | no |
| Network operations | none; the run was offline and loaded local files only |

The 4 optimizer steps ran at batch size 1 with 8 gradient-accumulation micro-batches,
i.e. 32 micro-batches over an 8-row training split. Because one optimizer step consumes
the whole split, 4 steps is 4 passes over it — the config asks for 1 epoch and
`max_steps` is what actually bounds the run.

Assistant-only loss was verified rather than assumed: the probe measured 27 prompt
tokens masked to the ignore index and 24 supervised completion tokens, and the
supervised span is the contiguous tail of the sequence. 5,046,272 parameters were
trainable out of 601,096,192.

## Dataset

| | |
|---|---|
| Dataset | `m62-defensive-smoke` `v1` |
| TRAIN | 8 |
| VALIDATION | 2 |
| HIDDEN_EVALUATION | 1 |
| SECURITY_REGRESSION | 1 |
| SFT export rows | 8 |

`HIDDEN_EVALUATION` and `SECURITY_REGRESSION` are bound into the plan by digest and are
not part of the training split.

## Identity

| | |
|---|---|
| TrainingPlan hash | `db6dd55b40106958897df92eefc37b2ab3f9f5711e4584c1adabba1418196286` |
| Adapter manifest hash | `06b1d3a304f29ecf49663daddb02d1c9d399d60fcc978894cb2b3f723b7c009c` |
| Artifact-set (tree) hash | `9918ac14d70647aace26c33ab16590ebe47a1a69a114b5e1e648758e66c2e070` |
| LoRA | rank 8, alpha 16, dropout 0.05, bias none, `CAUSAL_LM` |

The plan was computed offline, confirmed once with its exact `TRAIN:<plan-hash>` token,
and consumed once. The adapter, the run ledger and the generated config are runtime
artifacts: all of them are gitignored and none is tracked.

## Structural verification

The authoritative completed-run verifier returned no problems, and 40 independent checks
were run against the bytes on disk:

- every file named by the manifest rehashes to its recorded digest and size;
- the artifact-set hash recomputes from the files themselves;
- the run directory is flat — no nested tree, no symlink, no `.bin`/`.pt`/`.pth`/`.pkl`
  and nothing outside the adapter allowlist;
- the safetensors header parses to 392 tensors, every name is a LoRA tensor, no base
  model weights are dumped, and every tensor is finite;
- plan, config, base model, tokenizer, dataset manifest, train/validation shard,
  hidden-evaluation and security-regression digests all bind;
- `completed: true`, `interrupted: false`, requested steps equal completed steps;
- no username, hostname, cache path, credential or raw dataset row appears in any
  exportable record.

`target_modules` in the saved adapter config resolved to
`down_proj, gate_proj, k_proj, o_proj, q_proj, up_proj, v_proj`. PEFT resolves the
`all-linear` sentinel the plan approves against the loaded model and records what it
actually adapted; it never echoes the sentinel back.

## What it took to get here

Three earlier attempts failed, each exposing a real defect. Their evidence is preserved
untouched under ignored runtime storage and none of it was reused.

1. **Attempt 1** — failed during tokenization. On transformers 5,
   `apply_chat_template(tokenize=True)` returns a `BatchEncoding`, and `list()` of one
   yields its *keys*, so the prompt and the full sequence both "tokenized" to length 2
   and there was no completion left to supervise.
2. **Attempt 2** — `TrainingArguments` no longer accepts `save_safetensors` on
   transformers 5, which removed torch-pickle checkpoint serialization entirely. The
   `TypeError` escaped the handler, leaving the run in `RUNNING` with no terminal state,
   no quarantine and no terminal ledger line.
3. **Attempt 3** — trained all 4 steps successfully and then failed artifact validation
   on the shape of a real PEFT save: a nested checkpoint tree holding pickles, the model
   card PEFT writes on every `save_pretrained`, and the resolved `target_modules` list.

Two further defects were found by audit rather than by a failed run: the backend escape
check scanned the whole runs tree with no notion of when the backend held control, so any
earlier run — stale residue *or* a completed one — read as an escape and would have
failed every second run under one output root; and `from_pretrained` never received
`cache_dir`, so the planner verified one cache while the loader resolved against another.

Every one of these is now covered by a regression test.

## What this does not establish

- **Adapter quality is unevaluated.** No benchmark, no held-out scoring, no comparison
  against the base model has been run. The train loss above is a number the trainer
  reported, not evidence of an improvement.
- **No safety or capability claim is made.** Four optimizer steps over eight rows
  validates plumbing; it does not change what the model knows or refuses.
- **Candidate eligibility is unavailable.** It requires evaluation evidence that does not
  exist yet.
- **No promotion, activation, merge, conversion or upload is authorized**, and none was
  performed.

## Next

Live S3C baseline-versus-adapter evaluation.
