# V69 M62 S3I.1 — Kali-native evaluation runtime and the canonical eval-v2 lineage

**Status: `PASS`. D34 is `FIXED`, the Linux evaluation runtime is `QUALIFIED`, and the real
S3I plan builds with zero blockers. Nothing was generated. No `EVAL:` authority was created
or consumed. The candidate remains `TRAINED_UNEVALUATED`.**

This milestone closes the two blockers that stopped S3I at its pre-token gate. It is a
qualification milestone: it establishes that an evaluation *could* run, and deliberately
does not run one.

---

## 1. Authorisation and boundary

The operator authorised **M62 S3I.1 — Kali-native evaluation runtime qualification + D34
closure**, and in doing so **revoked the earlier Windows-host requirement**. Kali Linux is
now the host on which the first quality-candidate eligibility evaluation will run.

Explicitly authorised and done: a fresh clean checkout, migration of gitignored runtime
artefacts, a new Linux-native pinned virtual environment, load-only model qualification,
the D34 fix, its regression tests, the plan preview and its hash, docs, commit, push.

Explicitly **not** authorised and **not** done: any held-out generation, any
`model.generate()`, any forward pass, `EVAL:` token creation or consumption, training,
adapter mutation, scoring/grader/gate changes, D28/D29/D33 fixes, reasoning-policy or
`max_new_tokens` changes, loader sessionization, promotion, registry mutation, merge, tag,
release or version bump.

```
TOKENS_GENERATED         0
MODEL_FORWARD_PASSES     0
HELDOUT_TASKS_EXECUTED   0
EVAL_TOKEN_CREATED       NO
EVAL_TOKEN_CONSUMED      NO
EVAL_ATTEMPTS            0
```

---

## 2. Why there is a fresh checkout

The Kali checkout that ran the blocked S3I attempt shows **182 tracked files modified**,
72 417 insertions against 72 417 deletions, while `git diff --ignore-all-space` is
**empty**. Every one of those differences is a line ending: the index holds LF, the working
tree holds CRLF, there is no `.gitattributes` and `core.autocrlf` is unset. It is an
artefact of the repository having been copied from the Windows host M62 ran on.

That checkout was left **exactly as found** — nothing was reset, restored, stashed, cleaned,
normalised or committed from it. It was read for two things only: the authoritative remote
URL, and the gitignored runtime artefacts listed in §4.

All tracked work in this milestone happened in a **fresh clone** of the authoritative
repository, checked out at the same commit. Repository-wide line-ending policy is separate
work and no `.gitattributes` was added.

| | |
|---|---|
| Starting HEAD | `4d1b95448d584fec6a788d1c23845ca251777808` |
| Branch | `jarvis-v69-m62-training-gym` |
| `origin/<feature>` divergence at start | `0  0` |
| `origin/master` | `3705114228edef2f665be349c5c4429b7b16777a` — untouched |
| Fresh clone worktree at start | **clean** |

---

## 3. Blocker B1 — closed by qualifying a new Linux runtime

S3I found no generation runtime on this host: system Python had no `torch`, `transformers`
or `peft`, and both `.venv` and `.venv-training-smoke` are **Windows** trees
(`Scripts/*.exe`, `Lib/`, `home = C:\…\Python312`) that cannot execute on Linux. Under the
prior brief, installing anything was refused. The operator has now authorised provisioning,
so a new isolated environment was built rather than a Windows environment reused.

### 3.1 The qualified Kali runtime

| | Value |
|---|---|
| OS | Linux, Kali GNU/Linux Rolling |
| Kernel / arch | `7.0.12+kali-amd64`, `x86_64`, glibc 2.42 |
| Python | **CPython 3.13.14** |
| `torch` | **2.13.0+cpu** |
| `transformers` | **5.14.1** |
| `peft` | **0.20.0** |
| `safetensors` | 0.8.0 |
| `tokenizers` | 0.22.2 |
| `accelerate` | 1.14.0 |
| `numpy` | 2.5.2 |
| `huggingface_hub` | 1.27.0 |
| `jsonschema` | **4.26.0** |
| CUDA available | **False** |
| Device | **CPU** |
| Environment | isolated venv, gitignored; no global package was touched |

The three packages the production backend names — `RUNTIME_PACKAGES = ("torch",
"transformers", "peft")` — are installed at **exactly** the releases the S3H adapter
manifest records (`{"peft": "0.20.0", "torch": "2.13.0+cpu", "transformers": "5.14.1"}`).
Nothing was upgraded, nothing was substituted, and no `pip install -U` was run.

`jsonschema` is not in the adapter manifest's `package_versions`, but
`evaluation/scoring.py` refuses schema validity when it is absent, so it is a real
evaluation dependency. `4.26.0` was resolved from repository evidence — it is the version
present in **both** copied Windows environments — not chosen freely.

### 3.2 Python differs from the historical runtime, and that is recorded

The S3H Windows environment was Python 3.12.10; this one is 3.13.14. The project's own
authority permits it: `pyproject.toml` declares `requires-python = ">=3.11"` and pins no
interpreter beyond that. No second interpreter was installed to cosmetically match Windows.

```
HISTORICAL_WINDOWS_RUNTIME:                    REFERENCE_ONLY
S3I_KALI_RUNTIME:                              NEWLY_QUALIFIED
CROSS_PLATFORM_BYTEWISE_INFERENCE_EQUIVALENCE: NOT_CLAIMED
```

**No claim is made that this runtime reproduces the Windows one bit for bit.** The
experimental requirement is narrower and is met: both S3I arms run under *this* runtime,
and the only difference between them is the adapter.

### 3.3 Internal arm parity

Baseline and candidate share host, OS, Python, torch, transformers, tokenizer, base
weights, base revision, prompts, task order, reasoning policy, output budget, scoring, seed
policy and loader behaviour. The single difference is `base` versus `base + immutable S3H
LoRA`. The backend enforces this independently: `assert_identical_policies` refuses a pair
whose arms were configured differently, comparing the policy **digest** rather than the
object.

### 3.4 Offline sealing

After provisioning, all model access ran under `HF_HUB_OFFLINE=1`,
`TRANSFORMERS_OFFLINE=1`, `HF_HUB_DISABLE_TELEMETRY=1`, with `local_files_only=True` and
`trust_remote_code=False`. Network was used for Python packages only. **No model weight was
downloaded**; the reviewed local cache was the only source.

---

## 4. Runtime artefacts and the reviewed cache

Only gitignored material was migrated into the fresh clone — `jarvis/training_runs/`
(complete, including the ledger), `jarvis/training_gym_datasets/`, and the small evaluation
and training ledger state. No tracked source, doc, test, `.git` directory or Windows
virtualenv was copied. `git status --short` in the fresh clone remained **empty** after the
copy.

The reviewed model cache already present on this machine was reused. Its absolute path is
runtime state and is deliberately not recorded here.

| | |
|---|---|
| Base model | `Qwen/Qwen3-0.6B` |
| Revision | `c1899de289a04d12100db370d81485cdf75e47ca` — the only revision cached |
| Tokenizer | `Qwen/Qwen3-0.6B` @ same revision, `Qwen2Tokenizer`, vocab 151 643 |
| Chat-template digest | `a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8` ✅ |

The chat-template digest was **re-derived on Linux from the reviewed cache** and matches
the digest the S3H adapter manifest recorded on Windows exactly. That is the strongest
cross-platform parity evidence this milestone has, and it is why the tokenizer is not
treated as a new variable.

---

## 5. The immutable S3H candidate, re-verified

Verified **before** any runtime was provisioned, using pure structural and hash checks that
need no `torch`.

| | |
|---|---|
| Candidate | `qwen3-06b-lora-quality-live-001` |
| Status | `TRAINED_UNEVALUATED` (unchanged) |
| Adapter SHA256 | `43213035c15cd38928d2d6a3bdbd9af96872a954801c6bfd0a9b82a8e22ac858` ✅ |
| Adapter manifest hash | `1f76ccfbb8efc566c293ab6430d041dd24748035ed48aec6552d1e3bac24699f` |
| Artifact tree hash | `00aa57bbbe7f0af73501dae2330fb0b08682ede813843f92b26681ec77d659b6` |
| `verify_completed_run` | **PASS — 0 problems** |
| Bindings | plan `122efc62…`, config `b5f63cd8…`, dataset `9bbac2f0…`, template `a55ee1b1…` |
| LoRA | r 16, α 32, dropout 0.05, 7 target modules, `CAUSAL_LM`, peft 0.20.0 |

The adapter was not modified, merged, re-saved, renamed or requantised. Its SHA256 was
re-hashed after the load qualification and is unchanged.

---

## 6. Load-only model qualification — zero inference

Two bounded loads, no prompt, no forward pass, no `generate()`.

**Baseline.** `Qwen3ForCausalLM`, `config._name_or_path = Qwen/Qwen3-0.6B` at the pinned
revision, eval mode, CPU, fp32, 596 049 920 parameters, all finite, **no `peft_config`**,
released cleanly. Offline, no download.

**Candidate.** `PeftModelForCausalLM`, `active_adapters = ['default']`, **r 16 / α 32**,
target modules `{q,k,v,o,gate,up,down}_proj`, 196 LoRA layers, eval mode, CPU, base model
preserved as `Qwen3ForCausalLM`, **not merged** — `merge_adapter` was never called — adapter
SHA unchanged by the load, released cleanly. Offline, no download.

```
MODEL_LOADS_FOR_QUALIFICATION   2
MODEL_FORWARD_PASSES            0
TOKENS_GENERATED                0
```

The S3I.0 loader benchmark was **not** re-run. Its decision stands unchanged:
`KEEP_EXISTING_LOADING_STRATEGY`, `isolated_loads` / `PER_REQUEST`, an expected 36 + 36 = 72
loads in the live run.

---

## 7. Blocker B2 — D34, and how it was fixed

### 7.1 The operator ruling

```
D34_OPERATOR_DECISION:  CANONICALIZE_V2_PARENT_TO_V1
CANONICAL_V1_MANIFEST:  0970600c677c89112db972c6024634aa871be92dee303db7f429c90967d3dd3b
CANONICAL_V2_PARENT:    EXACT_CANONICAL_V1_MANIFEST
D32_STATUS:             SUPERSEDED_BY_D34
```

`10ad2308…` is **not corrupt**. It is the legitimate historical output of a genesis-lineage
construction produced by the defective implicit-parent rule. It is retained as history and
is disqualified only for future eligibility-grade authority.

### 7.2 Root cause, reproduced from the fresh clone

`PromotionRequest.parent_manifest_hash` defaults to `""`, and `resolved_parent()`
(`training_gym/datasets/promotion_plan.py`) then falls back to
`latest_manifest_hash(root, dataset_id)` — a **discovery over the destination root**:

```python
def resolved_parent(self) -> str:
    if self.parent_manifest_hash:
        return self.parent_manifest_hash
    return latest_manifest_hash(root=self.root, dataset_id=self.dataset_id)
```

`build_evaluation_corpus.py` never set the field. The genesis guard in `verify_parent`
(`manifests.py`) does not catch this, because its own condition — "does this root already
hold versions?" — is *also* incidental filesystem state. So the lineage of `v2` was decided
by what happened to be on disk.

### 7.3 Pre-fix reproduction matrix (measured here, not quoted)

| Build | `parent_manifest_hash` | `manifest_hash` |
|---|---|---|
| `v1` into a fresh root (control) | `genesis` | `0970600c…` ✅ reproduces |
| `v2` into a **fresh** root | `genesis` | **`10ad2308391567eeaa043001835b0c77a02473b26d2f83c0fb54a32d885b9df0`** |
| `v1` then `v2` into the same root | `0970600c…` | **`82b60bfdbea263eef3990eb6e49c2f2ca16e9b9e26ec8ac435f314b374279d60`** |

`diff -r` over the two built `v2` trees reports **exactly one differing file**,
`manifest.json`, and within it **exactly two differing fields**: `parent_manifest_hash` and
the `manifest_hash` derived from it. All three shards are byte-identical:

```
adversarial.jsonl          8738652dce7971f6176c84919c2496d0b02d970f65726d6d8da0d62e16b11e5a
hidden_evaluation.jsonl    d75fb843e6cd2f1791fed0cef6a8ee90b633206806fcb03031233ff69662f74a
security_regression.jsonl  af1dc5bb7235bc041920883f9cb72269944ae5a1f028d77c66f1790b1151614f
```

### 7.4 The fix, and why it is where it is

**One tracked source file changed: `jarvis/scripts/build_evaluation_corpus.py`.**

The corpus now **declares** its lineage instead of discovering one:

```python
CANONICAL_V1_MANIFEST = "0970600c…"

CANONICAL_LINEAGE: dict[str, tuple[str, str] | None] = {
    "v1": None,                            # a legitimate genesis
    "v2": ("v1", CANONICAL_V1_MANIFEST),   # an explicit parent
}
```

`build()` settles the lineage **before writing a single candidate**, and passes
`parent_manifest_hash` explicitly to `PromotionRequest`. A new helper,
`_materialize_canonical_parent`, guarantees the declared parent exists and *is* the declared
parent: present and correct → proceed; absent → build it from this same generator, then
check; present but **not** the declared digest → **refuse**.

The generic `latest_manifest_hash` default was deliberately **left alone**. Other datasets
legitimately chain onto their newest version, and removing that would be a dataset-subsystem
rewrite rather than a D34 fix. A version whose lineage nobody declared is now refused by
`canonical_parent_for` rather than silently defaulted to genesis.

**Fail-closed, not fall-back.** A build that cannot establish its declared parent raises and
leaves no version directory behind. It never degrades to `genesis` and never adopts whatever
version it happens to find — either of those mints a second identity for the same material,
which is the defect itself.

### 7.5 Post-fix determinism matrix

| Case | Root state | `parent` | `manifest_hash` |
|---|---|---|---|
| 1 | empty root → `v2` directly | `0970600c…` | `82b60bfd…` |
| 2 | empty root → `v1` → `v2` | `0970600c…` | `82b60bfd…` |
| 3 | root holding two **unrelated** datasets → `v2` | `0970600c…` | `82b60bfd…` |
| 4 | fresh independent root → `v2` | `0970600c…` | `82b60bfd…` |

**All four agree.** `v1` re-derives `0970600c…` with parent `genesis` in every root. The key
regression is closed: `v2` built into an otherwise clean root is **no longer a genesis**.

### 7.6 Canonical authority

```
EVAL_V2_DATASET_ID:                  m62-defensive-eval
EVAL_V2_VERSION:                     v2
CANONICAL_V1_MANIFEST:               0970600c677c89112db972c6024634aa871be92dee303db7f429c90967d3dd3b
CANONICAL_V2_PARENT:                 0970600c677c89112db972c6024634aa871be92dee303db7f429c90967d3dd3b
CANONICAL_V2_MANIFEST:               82b60bfdbea263eef3990eb6e49c2f2ca16e9b9e26ec8ac435f314b374279d60
HISTORICAL_GENESIS_V2_MANIFEST:      10ad2308391567eeaa043001835b0c77a02473b26d2f83c0fb54a32d885b9df0
```

Use the wording that does not falsify either digest:

```
10ad2308…   HISTORICAL_GENESIS_LINEAGE_IDENTITY
82b60bfd…   CANONICAL_V1_PARENTED_IDENTITY_FOR_FUTURE_ELIGIBILITY
```

---

## 8. The corpus did not change

| | |
|---|---|
| Total records | **36** |
| Splits | `hidden_evaluation` 12 · `security_regression` 12 · `adversarial` 12 |
| Families | `safety_refusal` 12 · `structured_report` 9 · `evidence_request` 9 · `tool_call_schema` 6 |
| Candidates built / promoted / rejected | 36 / 36 / 0 |
| Leakage | **CLEAN**, 0 findings |
| `evaluation_only` / `dataset_eligible` | `true` / `false` |
| `created_at_utc` | `2026-08-06T00:00:00Z` — unchanged |
| Shard bytes | **byte-identical** to the pre-fix build |

```
CORPUS_ROWS_CHANGED:    0
SHARD_BYTES_CHANGED:    NO
V2_CONTENT_CHANGED:     NO
```

### 8.1 Two derived digests moved, and exactly why

The **task-pack** hash for `v2` moved from `b4f9d6b1…` to
`3744a22e1866a40b6e5b27ae20e798365dfbf2d3c071018afba14bf611ec2665`, and
`task_identity_hashes` moved with it. This is **not** a content change and must not be read
as one.

A task record carries `source_dataset_manifest_hash` as its provenance, so the pack's
identity follows the dataset version's identity. Comparing all 36 task records across the
two lineages, **`source_dataset_manifest_hash` is the only field of 22 that differs.**
Identical in all 36: `user_prompt`, `system_prompt`, `task_hash`, `source_shard_hash`,
`expected_output_schema`, `tool_schemas`, `grader_ids`, `mandatory_grader_ids`,
`refusal_expected`, `security_required`, `kind`, `split`, `task_family`, `sensitivity`,
`lineage_group`, `evidence_ids`, `fixture_hashes`, `input_record_hash`, `evaluation_only`,
`pack_version`. **What the model is shown is unchanged.**

The `v1` task-pack hash `d714d89b…` is **unmoved**, which is the control proving the pack
builder itself did not change.

---

## 9. Regression tests

**New file:** `jarvis/tests/test_training_gym_m62_s3i1_canonical_eval_lineage.py` — 18
tests covering: the lineage is declared not discovered; an undeclared version is refused;
`v1`'s identity does not move; **`v2` in a clean root is not a genesis**; the declared parent
is materialised and verified; identity is independent of build order; unrelated dataset
state cannot change it; independent roots agree; a wrong declared parent is refused; a
corrupted parent is refused; lineage is part of manifest identity; shards are byte-identical
across both lineages; only provenance moves in the task pack; counts are untouched; leakage
stays clean; the `v2`-derives-from-`v1` relationship holds; a genuine genesis is still
representable; the generic `latest_manifest_hash` behaviour is unaffected.

**Non-vacuous, and demonstrated so:** run against the pre-fix generator, **11 of the 18
fail**, including `test_v2_in_a_clean_root_is_not_a_genesis` and
`test_v2_identity_is_independent_of_build_order`.

**Updated:** `test_training_gym_m62_s3f2_eval_corpus_v2.py` — `V2_MANIFEST_HASH` and
`V2_PACK_HASH` now carry the canonical values, each with the pre-D34 value and the reason
recorded inline. No assertion was weakened and no test was deleted.

---

## 10. Results

| Scope | Result |
|---|---|
| D34 regression file | **18 passed** |
| D34 + S3F.2 corpus v2 | **49 passed, 0 failed** |
| Focused dataset + evaluation files (11 files) | **674 passed, 0 failed** |
| Focused M62 (`-k m62`) | **2777 passed, 18 skipped, 0 failed** |
| **Full inner suite** | **6755 passed, 54 skipped, 0 failed, 0 errors** |
| Ruff (changed files) | **All checks passed** |
| `compileall` | OK |
| `git diff --check` | clean |
| Bandit (changed source + tests) | **118 findings — all LOW / B101 `assert_used`, 0 MEDIUM, 0 HIGH** |
| Secret / host-path / token-literal scan over added lines | clean |

**The one environmental caveat, measured rather than assumed.** On the bare system
interpreter the full suite reports **62 failed, 6651 passed, 55 skipped, 1 error** — and
**all 63** are `ModuleNotFoundError: No module named 'openai'`. `openai>=1.0.0` is a
declared *base* dependency (`pyproject.toml`) that is simply not installed here. It is not a
regression: stashing this milestone's changes and running the same files at pristine HEAD
reproduces the identical failures. Not one M62, dataset or evaluation test is among them.
Re-running the suite with `openai` available in a `--system-site-packages` venv gives the
green figure above. The global environment was **not** mutated to obtain it.

Per the standing warning in PROGRESS §15, these counts come from a **different host and
interpreter** than the S3G.2 baselines and are reported, **not** reconciled by arithmetic
against them.

The Bandit B101 findings are `assert` statements in test files. They were left exactly as
written: rewriting a test's assertions to satisfy a linter would weaken the tests to improve
a number.

---

## 11. The real S3I plan — PREVIEW, zero blockers

Built with `--dry-run` / `--print-plan`. `--execute` was never invoked and no confirmation
was supplied.

```
S3I_EVALUATION_PLAN_HASH:   dc8723b0391505687771d48f1c8d5d6031b77d5140ed179ebb80ecd5a15732f3
PLAN_BLOCKER_COUNT:         0
PLAN_WARNINGS:              0
IS_EXECUTABLE:              true
```

| Bound | Value |
|---|---|
| `evaluation_config_hash` | `5dd5737185b9be4b5ec6643eded1242b15e89f3472bdec924e56e75ccae38fdc` |
| `dataset_manifest_hash` | **`82b60bfd…`** — the D34 canonical `v2` |
| Shard digests | hidden `d75fb843…` · security `af1dc5bb…` · adversarial `8738652d…` |
| `task_pack_hash` (plan-time commitment) | `a41a10ea2ca48e0eb60927f8ec6facd022015eab1da543de2abc6a1e9f87d7a4` |
| `hidden_target_store_hash` | `b0824b91e6d195ce71a3cebd364afd36675b768852ea37160de642066c55c158` |
| `baseline_reference_hash` | `7ba92ab72cc906d01ae7fa96279cf7cfef837961cbf09f054667d45f15d6c0a9` |
| `candidate_adapter_reference_hash` | `0d65a7526e3a14558379e87c4e8a9bced355b3d08c2d9ee07c97237c4c581bbd` |
| `tokenizer_identity_hash` | `45894db983c6c827f9a3b0aa0d838875b1057f5f15d953789eace1a04e48e946` |
| `generation_policy_hash` | `c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7` |
| `grader_policy_hash` | `2059579278f42d159447b3f281df2fa5b34e058d03cf944f7f0b8547763447b2` |
| `metric_policy_hash` | `2d0830103bc11f280fc2a25e5ac8f0f79bd3e6a1ad589046d238e9fc5d9cfd87` |
| `statistical_policy_hash` | `663ebf65b73536fe3bd41043568a1f737ff751a43a964d14ff67c4e12662bf18` |
| `family_policy_hash` | `580fbe9104cbe684f702cba016e1191c83745fb8502642636c3fc885135065b1` |
| `gate_policy_hash` | `e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5` |
| `resource_policy_hash` | `0486300a3bca61717b0dd119721915709a4f34dd403f5ecdd45eb209bef65834` |
| `order_policy` / `order_assignment_hash` | `balanced_by_task_hash_and_seed` / `ac8096d7…` |
| `dependency_report_hash` | `78312447805c5b1a9a31e1d46f82b819c268ccbdbb0a217624c4b93676b74e3e` |
| `hardware_report_hash` | `627f088c64c2930aaab09a70ea16fe45379fdb4cf3bba016337d54307437bdfc` |
| `backend_id` / `evaluator_version` | `transformers_peft` / `m62.s3c.1` |
| Expected work | 36 tasks · 36 baseline + 36 candidate generations · 216 grader executions |

Generation policy as bound: `greedy_deterministic`, **`reasoning_policy = disabled`**,
**`max_new_tokens = 512`**, `max_input_tokens = 4096`, `do_sample false`, `temperature 0.0`,
`top_p 1.0`, `top_k 0`, `repetition_penalty 1.0`, `seed 11`, **`timeout_s = 300`**,
`batch_size 1`, `truncation_side refuse`, `device cpu`, `precision fp32`.
`eligibility_grade` is `true` and `generation.blockers()` is empty.

Declared effects, from the plan itself: runs a model `false`, downloads anything `false`,
contacts the network `false`, installs dependencies `false`, promotes a model `false`,
writes the registry `false`, executes tool calls `false`. Strongest possible verdict:
`eligible_for_human_review`.

### 11.1 Two things about this hash that must not be rediscovered

**It is not comparable to any Windows preview.** The plan legitimately binds runtime and
environment evidence — `dependency_report_hash`, `hardware_report_hash` and
`expected_output_root_id`, which is a digest of the output root path. A different host
produces a different plan hash *by design*. Do not force an older value.

**It binds the output root, so the live run must use the same clone.** `dc8723b0…`
reproduces from the fresh clone with the dataset root, training root and output root used
here. Running from a different directory changes `expected_output_root_id` and therefore the
plan hash. The live session must re-derive the plan and confirm it, not paste this digest in.

### 11.2 No EVAL authority was created

The CLI prints a `confirmation_required` string as part of the plan record; it is a pure
function of the plan hash and the repository derives it for any plan. **No token was
created, requested, stored or consumed, and `--execute` was never called.** The evaluation
config authored for this preview is runtime material and lives outside the repository, per
the runtime artefact policy.

---

## 12. Rulings carried forward unchanged

**D32** — `SUPERSEDED_BY_D34`. Its old reading, that the discrepancy was a *documentation*
defect, was incomplete: the root cause is
`LINEAGE_DEPENDENT_ON_INCIDENTAL_BUILD_STATE`. Historical reports stand as written.

**D33** — untouched. `timeout_s = 300` is declared and now bound to the plan;
`TIMEOUT_ENFORCEMENT: NOT_IMPLEMENTED`; `timeout_rate` remains structurally vacuous and must
never be cited as eligibility evidence. No asyncio timeout, no subprocess bound, no
enforcement of any kind was added. `D33_BLOCKS_S3I: NO`.

**D28** — `TOOL_CALL_CAPABILITY: NOT_QUALIFIED`. The backend populates no
`proposed_tool_calls`; `tool_call_validity_rate` is vacuous and the six `tool_call_schema`
tasks cannot decide eligibility. **OPEN, not fixed.**

**D29** — `looks_like_refusal` recognises sixteen literal phrasings the held-out JSON
refusal targets do not contain. **OPEN, not fixed.**

**Scoring, graders, body-free review evidence, the security scanner, the bootstrap and every
S3G §6 gate (SV-1…SV-9, QG-1…QG-4, FG-1…FG-4, OG-1…OG-7) are unchanged and were not
touched.** The measurement instrument is frozen; no post-training metric change was made.

Because nothing was generated, **every** S3G gate is `NOT_EVALUATED`. None is `PASS`, none
is `FAIL`, and none may be inferred.

---

## 13. Historical compatibility

Verified unaffected: `m62-defensive-eval v1` (`0970600c…`, and its pack `d714d89b…`), the
smoke and quality training corpora, the S3H adapter and all its bindings, and the historical
S3E.2 artefacts. Existing files on disk were not rewritten — what becomes canonical is the
**rebuild semantics**. The runtime dataset root already held `v2` under the parented
lineage, and it verifies to `82b60bfd…` unchanged.

---

## 14. S3I readiness

| Gate | Result |
|---|---|
| Fresh Kali checkout clean | ✅ |
| Kali-native venv qualified | ✅ |
| Base model offline load | ✅ PASS |
| Candidate offline load | ✅ PASS |
| Adapter SHA | ✅ match |
| Tokenizer/template digest | ✅ match |
| Base revision | ✅ match |
| D34 | ✅ FIXED |
| Canonical `v2` parent = `v1` | ✅ |
| `v2` identity build-order independent | ✅ |
| `v2` content changed | ✅ NO |
| Leakage | ✅ CLEAN |
| Reasoning DISABLED / 512 / 300 bound | ✅ |
| Timeout enforcement unchanged | ✅ |
| Loader strategy unchanged | ✅ |
| Scoring / gates unchanged | ✅ |
| Plan blockers | ✅ 0 |
| Plan hash derived | ✅ |
| EVAL token created | ✅ NO |
| Tokens generated | ✅ 0 |

```
S3I_READY:  YES
```

**The live evaluation is still not authorised by this milestone.** A separate explicit
operator authorisation starts the 72-generation run. The one-run authorisation remains
**unspent**.

---

## 15. What future sessions must NOT redo

- **DO NOT** reopen why `10ad2308…` and `82b60bfd…` differ — §7 settles it.
- **DO NOT** call `10ad2308…` corrupt. It is a legitimate historical genesis build.
- **DO NOT** use the genesis-`v2` identity for future eligibility.
- **DO NOT** make canonical `v2` lineage depend on filesystem state again.
- **DO NOT** remove the explicit `v1` parent without a new schema decision.
- **DO NOT** read the moved `v2` task-pack hash as a corpus change — see §8.1.
- **DO NOT** use the old CRLF-dirty checkout for tracked development, and do not copy
  tracked files from it into a clean clone.
- **DO NOT** reuse the Windows virtualenvs on Linux.
- **DO NOT** call the Kali runtime identical to the historical Windows runtime.
- **DO NOT** re-run the S3I.0 loader benchmark.
- **DO NOT** change `reasoning_policy` from `DISABLED` or `max_new_tokens` from `512`.
- **DO NOT** fix D33, D28 or D29 inside S3I.
- **DO NOT** alter scoring, graders or the S3G gates.
- **DO NOT** mutate the S3H adapter.
- **DO NOT** create `EVAL:` authority before the explicit live-S3I authorisation.

---

## 16. NEXT

```
M62 S3I — FIRST QUALITY-CANDIDATE HELD-OUT ELIGIBILITY EVALUATION
KALI-NATIVE QUALIFIED RUNTIME
72-GENERATION AUTHORIZATION STILL UNUSED
AWAITING EXPLICIT OPERATOR AUTHORIZATION

MODEL_PROMOTION:          NOT_AUTHORIZED
MODEL_REGISTRY_MUTATED:   NO
CANDIDATE_ELIGIBILITY:    NOT_ESTABLISHED
```
