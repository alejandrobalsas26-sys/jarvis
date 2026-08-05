# V69 M62 — First Live Adapter Evaluation: Attempted, Blocked, Not Performed

**UTC date:** 2026-08-04
**Status:** `LIVE_ADAPTER_EVALUATION: BLOCKED`
**No base-versus-adapter comparison has been performed on this repository.**

This document records an *attempt*, not a result. It exists because the attempt
established exactly where the boundary is, and the next session should not have to
rediscover it. Nothing here is evidence about adapter quality.

---

## 1. What was attempted

A live comparison of the pinned base model against the same base model plus the
verified run-004 LoRA adapter, driven through the existing S3C CLI
(`python -m scripts.evaluate_adapter --execute`).

## 2. Outcome

Every precondition the CLI can check passed. The command then refused:

```
exit_code 18 (EXIT_BACKEND)
"the live evaluation path for plan e67687ae357f is not enabled in this build.
 Every precondition this command can check has passed; running a real
 base-versus-adapter comparison is the next milestone's boundary and has never
 been performed on this repository. Nothing was loaded and nothing was written"
```

This is not a misconfiguration and not an environment failure. `_execute` in
`scripts/evaluate_adapter.py` performs its preflight checks and then returns an
unconditional refusal. No code path in that file reaches `get_backend`,
`run_paired_evaluation`, `create_generation_directory`, `consume_plan` or
`write_evaluation_artifacts`.

**Consequences, stated plainly:**

| Field | Value |
|---|---|
| `EMPIRICAL_STATUS` | `INSUFFICIENT_EVIDENCE` |
| `ADAPTER_QUALITY_RESULT` | `INSUFFICIENT_EVIDENCE` |
| `SECURITY_REGRESSION` | `INSUFFICIENT_EVIDENCE` |
| `CANDIDATE_ELIGIBILITY` | `NEEDS_MORE_EVIDENCE` |
| `MODEL_REGISTRY_PROPOSAL` | `NOT_CREATED` |
| `MODEL_PROMOTION` | `NOT_AUTHORIZED` |

No `EvaluationReport` exists. No evaluation generation directory was created, no
ledger line was appended, and the plan was **not** consumed — the token remains
unspent and the same plan can be re-planned later.

## 3. What *was* verified (and is trustworthy)

These are real results from the authoritative verifiers, not assertions.

**Adapter — run `qwen3-06b-lora-smoke-live-004`**
Re-verified from disk through `verify_completed_run` via `build_adapter_reference`:

- `adapter_artifacts_reverified: true`
- run state `completed`, `interrupted: false`, `completed: true`, method `sft_lora`
- candidate reference hash `dfd8c2cbb0b6e7cf6ea1a47452f698b804ced812a749b92418bf83933bd9a650`
- six allowlisted files only; `adapter_model.safetensors` 20,236,472 bytes
- train loss 3.283784 over 4 completed steps, CPU/FP32,
  `local_files_only: true`, `trust_remote_code: false`

**Baseline** — `Qwen/Qwen3-0.6B` @ `c1899de289a04d12100db370d81485cdf75e47ca`,
revision kind `immutable_commit`, cache status `present`.

**Reference pair** — `pair_ok: true`, `blockers: []`, `live_execution_blockers: []`.

**Plan** — `plan_hash e67687ae357f1ab3361ef874f5c208c3b6cb207a9ff029bb7a8faf3741d9a546`,
`is_executable: true`, `expected_task_count: 4`, `runs_a_model: false` at plan time.

**Held-out evidence available**

| Split | Tasks |
|---|---|
| `validation` | 2 |
| `hidden_evaluation` | 1 |
| `security_regression` | 1 |
| `adversarial` | 0 (no such shard exists) |

Task-family distribution: `structured_report: 12` — the entire dataset. Dataset
manifest hash `61b3c146aae6e7d1e8e627fc617d41f984b134eb46949e17c701c5e9e382826a`,
matching the hash recorded in the adapter manifest.

## 4. Defects found and fixed this session

**Checkpoint policy (`fix(training): block unsupported pickle-bearing trainer checkpoints`)**
`checkpoint_strategy` accepted `epoch`/`steps` and *defaulted* to `epoch`, including in
the shipped smoke template. Both ask the trainer for a nested `checkpoint-<step>/`
directory holding `optimizer.pt`, `scheduler.pt`, `rng_state.pth` and
`training_args.bin` — every one refused by the adapter artifact allowlist. The
configuration was a guaranteed wasted run. Now refused at config validation, which
precedes planning, plan consumption, run-directory creation, model import and network.
The artifact validator was not weakened; it was already correct.

**Chat-template return shape (`fix(evaluation): pin the chat-template return shape…`)**
`apply_chat_template` was called without `return_dict`. transformers 5 defaults it to
`True` and returns a `BatchEncoding`, which has no `.shape` — so `int(encoded.shape[-1])`
raises `AttributeError`, and passing it positionally to `generate` hands a mapping to a
tensor parameter. Verified against the pinned tokenizer on transformers 5.14.1.
This is the first defect on the live path found by loading the real tokenizer.

**Execution-stage metadata** — `TrainingPlan.expected_effects()` still reported
`s3b_not_implemented` after S3B trained a real adapter. Corrected. The key sits outside
`to_dict()` and therefore outside the plan hash, so already-recorded runs keep verifying;
this was confirmed by re-verifying run 004 after the change.

## 5. Known defects reported but deliberately NOT fixed

**The dependency gate cannot block.** All three `build_dependency_report(...)` call sites
in `scripts/evaluate_adapter.py` omit `method=`, so `method_packages` is empty and
`DependencyReport.ready` is vacuously `True` regardless of what is installed. S3A/S3B
pass `method=` correctly. Left alone because it is permissive rather than blocking, and
closing it is a scope decision for the session that wires the live path.

**`identity_hash` conflates identity with annotation.** It covers `cache_status`,
`cache_evidence` and `license_reference`. The shipped template
`evaluation/configs/qwen3-0.6b-adapter-eval.json` sets
`license_reference: "see the model card on the hub"`, while `identity_from_config`
records `""` — so the template guarantees a base-model identity mismatch against every
S3B-trained adapter. Reproduced exactly: with `license_reference=""` and cache present
the hash is `9701f4f3368d…`, matching the adapter manifest; with the template's string it
is `042df2494388…`. Left alone because the durable fix changes what the hash covers,
which would invalidate recorded hashes.

## 6. What blocks eligibility even once the live path exists

These are structural, and no amount of measurement changes them:

- **Sample size.** 4 paired tasks against `min_pairs_for_claim = 30`. The bootstrap
  verdict can only be `SMALL_SAMPLE`, which does not support a directional claim, so
  eligibility can at best reach `NEEDS_MORE_EVIDENCE`.
- **Mandatory families absent.** `TaskFamilyPolicy.mandatory_families` requires
  `safety_refusal` and `tool_call_schema`. The dataset contains neither, so
  `family_findings()` emits two blocking `mandatory_family_absent` findings.
- **No required-refusal task.** `EvaluationTaskPack.eligibility_blockers()` requires at
  least one. Without it, a model that refuses everything is indistinguishable from a safe
  one.
- **No adversarial evidence.** No `adversarial` shard exists.
- **No pack builder.** Nothing loads shard records into `DatasetCandidate` objects or
  assigns graders, output schemas and tool schemas per task. `build_task` handles one
  record; the orchestration above it does not exist.

## 7. Threshold calibration status

**Uncalibrated.** Every gate threshold still carries `thresholds_are_calibrated: False`.
No measured distribution exists to calibrate against, because no evaluation has run.

## 8. Model Registry status

Untouched. No proposal was created, nothing was promoted or activated, and no model role
changed. `EvaluationPlan` structurally refuses to represent any of those effects.

## 9. Exact next steps

1. Wire `_execute` to the real pipeline: generation directory, plan consumption, pack and
   hidden-target store construction, both backends, `run_paired_evaluation`, scoring,
   comparison, metrics, statistics, gates, report, artifact writing and post-write
   verification.
2. Build the missing shard → `DatasetCandidate` → `build_task` pack builder, including
   per-task grader ids, expected output schema and tool schemas.
3. Pass `method=` at the three dependency-report call sites.
4. Grow held-out evidence: `safety_refusal` and `tool_call_schema` families, an
   `adversarial` split, and enough paired tasks for the statistical policy to speak.
5. Only then run the live comparison — and expect `NEEDS_MORE_EVIDENCE` until item 4 is
   genuinely satisfied.

---

*No claim of production readiness, safety certification, improvement, generalization,
promotion or activation is made or implied by this document.*
