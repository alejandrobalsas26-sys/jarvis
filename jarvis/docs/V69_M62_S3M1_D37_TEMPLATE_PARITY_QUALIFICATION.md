# V69 M62 S3M.1 — D37 train/eval chat-template parity qualification

> **Status: `D37_STATUS: FIXED`.** The defect was reproduced against the reviewed pinned
> chat template, characterised to the exact token, measured against all ten predeclared
> closure criteria, and closed with the minimum train-side production change.
> **Zero training, zero evaluation, zero model generations, zero optimizer steps, no
> `TRAIN` or `EVAL` authority, no candidate 003, no `eval-v4`.**

| | |
|---|---|
| Milestone | V69 M62 **S3M.1** — D37 only |
| Date | 2026-08-15 |
| Branch | `jarvis-v69-m62-training-gym` |
| Starting HEAD | `06480cbc628dd6edb3c689611a16d34d5d3dc18f` |
| Master | `3705114228edef2f665be349c5c4429b7b16777a` (untouched) |
| Preceding milestone | S3M — `jarvis/docs/V69_M62_S3M_STRUCTURED_OUTPUT_DIAGNOSIS.md` |

---

## 1 — Authorisation and boundary

The human operator authorised **D37 as its own milestone**, after S3M explicitly refused
to fix it as a rider on a diagnosis. What was authorised: reproduce D37, characterise it
at token level, decide whether it is a real reproducibility defect, and — **only if ten
predeclared closure criteria all passed** — implement the minimum correctness fix with
non-vacuous regression tests.

**What this milestone did not do**, measured rather than asserted:

```
TRAIN_TOKEN_CREATED / CONSUMED:   NO / NO
EVAL_TOKEN_CREATED / CONSUMED:    NO / NO
MODEL_GENERATIONS:                0
MODEL_RESPONSE_TOKENS_GENERATED:  0
OPTIMIZER_STEPS:                  0
ADAPTERS_CREATED / MUTATED:       0 / 0
MODEL_WEIGHTS_LOADED:             NO   (a tokenizer and a Jinja template only)
CANDIDATE_003_CREATED:            NO
EVAL_V4_CREATED:                  NO
HELD_OUT_TASK_BODIES_READ:        NO
D38 / D39:                        OPEN_UNCHANGED — no production behaviour touched
GATES / GRADERS / THRESHOLDS:     UNCHANGED  (`e5003319…` re-derived, zero drift)
EVALUATION_REASONING_POLICY:      DISABLED, unchanged and still explicit
MAX_NEW_TOKENS:                   512, unchanged
```

**Sealed and not reopened:** the S3I verdict, the S3L verdict, candidate 001, candidate
002, D34, D35, D36, and S3M's structured-output diagnosis. Candidate 001 and candidate
002 remain `EVALUATED_NOT_ELIGIBLE`, measured under the instrument actually used, and
this milestone does not rescore, reinterpret or retroactively invalidate either.

---

## 2 — D37, stated exactly

`apply_chat_template` is a **function**. `tokenizer_chat_template_hash` digests its
**source**. So two subsystems can record the identical digest and hand the model
different bytes — and that is what M62 did:

| | training backend | evaluation backend |
|---|---|---|
| call | `apply_chat_template(…, add_generation_prompt=True)` | `apply_chat_template(…, add_generation_prompt=True, enable_thinking=False)` |
| `enable_thinking` | **not passed** | passed, from the plan-bound `reasoning_policy` |
| effective policy | the template's own default | **`DISABLED`** (operator ruling H6a) |
| recorded anywhere? | **no** | in `generation_policy_hash` → `parity_hash` |

S3M recorded the consequence and left the causal weight open. S3M.1 measures the whole
of it.

---

## 3 — Tokenizer authority: how the reviewed template was reached

The reviewed model cache was **not** reachable from repository authority this session.
`scripts/qualify_reasoning_policy.py::locate_cache` — the repository's own locator, which
reads one explicit argument, three Hugging Face environment variables and the single
documented default, and **never walks the filesystem** — returned no root. No home
directory was swept and nothing was downloaded.

The reviewed **chat template** was nevertheless recovered, from repository state rather
than from the host: the S3D attempt-3 quarantine directory
(`training_runs/quarantine/qwen3-06b-lora-smoke-live-003-7e9b2593/checkpoint-4/`) is
preserved untouched under ignored runtime storage, and it carries the `chat_template.jinja`
and `tokenizer.json` that run wrote when it loaded `Qwen/Qwen3-0.6B @ c1899de2…` offline.

It was **verified before it was used**, not assumed:

```
chat_template.jinja, read with universal newlines  ->  4168 chars
sha256                                             ->  a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8
PINNED DIGEST OF RECORD                            ->  a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8
MATCH                                              ->  YES
```

The file on disk carries CRLF line endings — the documented Windows-copy artefact
(PROGRESS §14.48) — so it hashes to `51fa65c7…` byte-for-byte and to the pinned
`a55ee1b1…` when read as text. **Only the digest-verified form was used.** Three files
were copied read-only into a scratch directory and the quarantine was never written to.

```
PINNED_TOKENIZER_USED:            YES
TOKENIZER_CLASS:                  Qwen2Tokenizer  (transformers 5.14.1, .venv-m62-train-linux)
BASE_MODEL / REVISION:            Qwen/Qwen3-0.6B @ c1899de289a04d12100db370d81485cdf75e47ca
eos_token / id:                   <|im_end|> / 151645     pad: <|endoftext|> / 151643
OFFLINE:                          HF_HUB_OFFLINE=1 · TRANSFORMERS_OFFLINE=1 ·
                                  HF_HUB_DISABLE_TELEMETRY=1 · local_files_only=True ·
                                  trust_remote_code=False · nothing downloaded
MODEL_WEIGHTS_LOADED:             NO
```

---

## 4 — The call-site matrix

Every production use of `apply_chat_template` in the repository, classified. There are
**four** call sites in three files, not two.

| # | Location | Class | `tokenize` | `add_generation_prompt` | `enable_thinking` | Consumer |
|---|---|---|---|---|---|---|
| 1 | `training/backends/transformers_peft.py::_encode` (prompt) | **TRAINING** | `True` | `True` | **was: absent** → now: from `config.reasoning_policy` | `build_labels` prompt span → loss mask |
| 2 | `training/backends/transformers_peft.py::_encode` (full) | **TRAINING** | `True` | `False` | **was: absent** → now: same policy | `input_ids` + `labels` → `Trainer` |
| 3 | `evaluation/backends/transformers_peft.py::generate` | **EVALUATION** | `True` | `True` | from `policy.reasoning_policy.template_kwarg` — **unchanged** | `model.generate` |
| 4 | `training/chat_render.py::template_honours_reasoning_policy` (×2 renders) | **OTHER** (honour check) | `False` | `True` | literal `True` / `False`, deliberately both | a comparison, never trained or generated on |
| 5 | `scripts/qualify_reasoning_policy.py` | **OTHER** (offline preflight) | `False` | `True` | all three policies, deliberately | a digest table, no model |

Call site 4 moved in this milestone: it was inside the evaluation backend and is now the
shared authority both backends call. Its behaviour is unchanged and both historical
names still resolve from the evaluation backend.

---

## 5 — The render matrix, against the reviewed template

Six wholly synthetic fixtures. Five are the shapes production conversion accepts
(`[user, assistant]` and `[system, user, assistant]`); the sixth is multi-turn, which
**production does not support** — `_check_messages` accepts exactly those two shapes —
and is rendered only to record template semantics.

**Prompt prefix** (`add_generation_prompt=True`):

| fixture | default chars / sha256[:16] | DISABLED chars / sha256[:16] | delta | full-sequence identical |
|---|---|---|---|---|
| `A_benign_user_only` | 120 / `4bdb12f85fcedb2e` | 139 / `ef6a547e57d48f9f` | +19 | YES |
| `B_safe_cyber_system_user` | 209 / `8ed743c6652626d4` | 228 / `9f8013f7aca992ad` | +19 | YES |
| `C_strict_json_user_only` | 203 / `89780c2680df1c78` | 222 / `fbc6a9c1d2c115c1` | +19 | YES |
| `D_refusal_shaped_user_only` | 143 / `757eccde58190059` | 162 / `6c89b57be99b6cfb` | +19 | YES |
| `F_system_user_json` | 184 / `d1d96f1c5f7b1a21` | 203 / `2e1fded81a2ec87d` | +19 | YES |
| `E_multi_turn_NOT_PRODUCTION` | 211 / `6c905a72884e9455` | 230 / `8d9f56325e71c0bc` | +19 | YES |

`MODEL_DEFAULT` and `ENABLED` render **byte-identically** on every fixture — the S3F.2
addendum's finding, reproduced. `DISABLED` differs on every fixture, by **exactly 19
characters, every time**.

**S3F.2's recorded 79 → 98 chars is the same +19.** Those values are not reproduced here
because they were taken on a different probe prompt; the *delta* is the invariant, and it
reproduces exactly.

---

## 6 — The semantic difference, named

Not "the strings differ". The reviewed template's final stanza is:

```jinja
{%- if add_generation_prompt %}
    {{- '<|im_start|>assistant\n' }}
    {%- if enable_thinking is defined and enable_thinking is false %}
        {{- '<think>\n\n</think>\n\n' }}
    {%- endif %}
{%- endif %}
```

```
CATEGORY:                         GENERATION MARKER / ASSISTANT-TURN PREFIX
EXACT DELTA:                      '<think>\n\n</think>\n\n'   (19 chars, 4 tokens)
TOKEN IDS:                        [151667, 271, 151668, 271]
SYSTEM PREFIX CHANGE:             NO
CONTROL / SPECIAL TOKEN CHANGE:   NO   (<|im_start|>, <|im_end|> unmoved)
NEWLINE LAYOUT CHANGE:            only inside the delta above
ASSISTANT TURN START:             unchanged — `<|im_start|>assistant\n` in both
EOS / TERMINATOR EXPECTATION:     unchanged — `<|im_end|>` (151645) in both
ANSWER TARGET POSITION:           MOVES, by exactly those 4 tokens
```

`enable_thinking` is read **only** inside the `add_generation_prompt` branch. Nothing
else in the template consults it. This was read from the digest-verified source, not
inferred.

### 6.1 — The consequence nobody had measured

The template emits `<think>\n\n</think>\n\n` in front of the **final assistant message
unconditionally** — that lives in the message loop, not in the generation-prompt branch:

```jinja
{{- '<|im_start|>' + message.role + '\n<think>\n' + reasoning_content.strip('\n')
    + '\n</think>\n\n' + content.lstrip('\n') }}
```

So the **full supervised sequence is byte-identical under both policies**, and only the
*prompt prefix* moves — which is precisely where `build_labels` puts the loss boundary.
Therefore, before this milestone:

* the training prompt prefix was **4 tokens short** of the evaluation prompt, and
* those 4 tokens fell on the **supervised** side of the mask, so **training supervised an
  empty reasoning-control sequence** that, at evaluation time, was already in the prompt.

That is D37's real shape, and it is stronger than "two strings differ".

---

## 7 — Token-level parity, and the supervised span

| fixture | prompt tok legacy → DISABLED | first divergence | full tok | supervised legacy → DISABLED |
|---|---|---|---|---|
| `A_benign_user_only` | 23 → 27 | index 23 | 53 = 53 | 30 → 26 |
| `B_safe_cyber_system_user` | 37 → 41 | index 37 | 77 = 77 | 40 → 36 |
| `C_strict_json_user_only` | 39 → 43 | index 39 | 79 = 79 | 40 → 36 |
| `D_refusal_shaped_user_only` | 24 → 28 | index 24 | 66 = 66 | 42 → 38 |
| `F_system_user_json` | 34 → 38 | index 34 | 64 = 64 | 30 → 26 |
| `E_multi_turn_NOT_PRODUCTION` | 43 → 47 | index 43 | 65 = 65 | 22 → 18 |

The first divergence index equals the legacy prompt length on every fixture: the DISABLED
prompt is a **pure suffix extension** of the legacy one. Common prefix = the whole legacy
prompt; the 4 added tokens are always `[151667, 271, 151668, 271]`.

Decoded supervised span, legacy vs DISABLED (fixture `C`):

```
legacy   : '<think>\n\n</think>\n\n{"schema_version": "1.0", "decision": …}<|im_end|>\n'
DISABLED : '{"schema_version": "1.0", "decision": …}<|im_end|>\n'
```

### The four required answers (§15)

```
TARGET_BYTES_CHANGE:              NO   — the full render is byte- and token-identical
TERMINATOR_TOKEN_CHANGE:          NO   — <|im_end|> (151645) in both, still supervised
PROMPT_PREFIX_CHANGE:             YES  — +4 tokens, a pure suffix extension
ASSISTANT_START_BOUNDARY_CHANGE:  YES  — the mask boundary moves forward by those 4 tokens
```

The authored corpus is untouched. What changes is **which side of the loss mask the
template's own reasoning-control sequence falls on**.

---

## 8 — Masking, terminator and thinking-markup proofs

**Masking parity (§16)** — the production `_masking_self_test`, over all five
production-shaped fixtures encoded by the production `_encode`:

```
CURRENT  (implicit default)  verified=True  problems=[]  probe 23 prompt / 30 completion
PROPOSED (explicit DISABLED) verified=True  problems=[]  probe 27 prompt / 26 completion
strategy manual_label_masking(-100) in both
prefix property (full starts with prompt): holds in BOTH   check_masking: 0 problems in BOTH
```

**Terminator parity (§32)** — no generation performed:

```
EXPECTED_ASSISTANT_TERMINATOR_ID: 151645  (<|im_end|>, and the tokenizer's eos_token_id)
SUPERVISED_TERMINATOR_PRESENT:    YES, on every fixture, under BOTH policies
SUPERVISED_TERMINATOR_ID:         151645
MATCH:                            YES
POSITION:                         second-to-last supervised token; the last is 198 ('\n'),
                                  the template's inter-turn separator, also supervised
CLOSING JSON BRACE SUPERVISED:    YES  (token 9207 = '"}' on both JSON fixtures)
```

**Thinking-markup proof (§33)** — stated exactly, not vaguely:

```
Does the template emit a reasoning-control sequence under DISABLED?  YES.
What is it?                                                          '<think>\n\n</think>\n\n'
Where, before the fix?   in the SUPERVISED span (the prompt stopped short of it)
Where, after the fix?    in the PROMPT PREFIX, masked — exactly where evaluation puts it
Does evaluation see the same prefix after the fix?                   YES, byte- and token-identical
Is any reasoning payload supervised after the fix?                   NO
```

**Truncation (§C7)**: the full sequence does not change length at all, so no row can be
newly truncated. Measured: identical `len(input_ids)` per fixture under both policies,
0 rows over 512 in both.

---

## 9 — Prefix-parity proof (§31), the decisive measurement

For the same synthetic system/user history, the training-side rendered prefix immediately
before the supervised assistant answer, against the evaluation-side rendered prompt
immediately before generation, under `reasoning_policy = DISABLED`:

| fixture | BEFORE (bytes / tokens) | AFTER (bytes / tokens) |
|---|---|---|
| `A_benign_user_only` | FAIL / FAIL | **PASS / PASS** |
| `B_safe_cyber_system_user` | FAIL / FAIL | **PASS / PASS** |
| `C_strict_json_user_only` | FAIL / FAIL | **PASS / PASS** |
| `D_refusal_shaped_user_only` | FAIL / FAIL | **PASS / PASS** |
| `F_system_user_json` | FAIL / FAIL | **PASS / PASS** |

```
TRAIN_EVAL_PREFIX_PARITY_BEFORE:  FAIL
TRAIN_EVAL_TOKEN_PARITY_BEFORE:   FAIL
TRAIN_EVAL_PREFIX_PARITY_AFTER:   PASS
TRAIN_EVAL_TOKEN_PARITY_AFTER:    PASS
```

**The comparable region was defined correctly.** Training renders prompt **plus** the
authored answer; evaluation renders the prompt **only**. Comparing those two directly is
a test that can only fail, and one that passes would mean the corpus had lost its
targets. The compared object is the training render's prefix up to the supervised answer
— which is exactly the `prompt_ids` the backend masks against — and the test asserts
separately that the training row still carries a non-empty supervised answer.

---

## 10 — The identity defect, and what was insufficient

```
tokenizer_chat_template_hash a55ee1b1…   digests the template SOURCE
                                          identical on BOTH sides across S3H/S3K/S3I/S3L
                                          CANNOT distinguish the two renderings
```

Precisely which identities were insufficient, before this milestone:

| Identity | Bound the reasoning policy? |
|---|---|
| `TrainingConfig.config_hash()` | **no** — no such field existed |
| `TrainingPlan.plan_hash()` | **no** — it binds the config hash, which did not carry it |
| `AdapterManifest.manifest_hash()` | **no** |
| `tokenizer_chat_template_hash` | **no** — source only |
| `GenerationPolicy.policy_hash()` (evaluation) | **yes** — the evaluation side was already correct |
| `parity_hash()` (evaluation, cross-arm) | **yes**, transitively |

So the asymmetry was total: evaluation bound it and training had no concept of it.

---

## 11 — Predeclared closure criteria (evaluated BEFORE production was edited)

Every measurement in §5–§10 was taken before a line of production code changed.

| | Criterion | Result | Evidence |
|---|---|---|---|
| **C1** | divergence reproduced from source and/or the pinned tokenizer | **PASS** | both: template source read (§6) and rendered (§5), digest `a55ee1b1…` verified |
| **C2** | caused by implicit training default vs explicit evaluation policy | **PASS** | call-site matrix §4; `MODEL_DEFAULT == ENABLED != DISABLED` §5 |
| **C3** | evaluation policy stays frozen at `DISABLED`, does not need to move | **PASS** | untouched; `c6b0b682…` re-derived byte-identical |
| **C4** | training can bind `DISABLED` without altering authored dataset content | **PASS** | full render byte-identical; no corpus row read or written |
| **C5** | masking remains correct | **PASS** | §8 — production self-test `verified=True`, 0 problems, both policies |
| **C6** | assistant target content semantically identical | **PASS** | §7 — decoded supervised span *equals* the authored target under DISABLED |
| **C7** | no truncation introduced | **PASS** | §8 — identical sequence lengths, 0 of 5 over 512 |
| **C8** | a future plan can bind the policy deterministically | **PASS** | §13 preview — `config_hash` and `plan_hash` both move, reproducibly |
| **C9** | a machine-testable render identity distinguishes implicit vs DISABLED | **PASS** | §12 — `chat_render_policy_hash`, 4 property tests |
| **C10** | no candidate weights, evaluation outputs or historical identities rewritten | **PASS** | §14 — every historical digest re-derived unchanged |

**All ten passed, so the fix was implemented.** Had any been ambiguous the milestone
would have closed `QUALIFIED_NOT_FIXED` with production changes at zero.

---

## 12 — What was implemented

**Five production files changed, one added.** No unrelated cleanup.

### 12.1 — `training_gym/training/chat_render.py` (new, the shared authority)

The single place that says what a render call *means*. It holds `ReasoningPolicy`
(**moved** from `evaluation/generation.py`), the one policy→kwargs mapping, the one
template-honour check, and `ChatRenderPolicy`.

**Why the training side owns it:** the package dependency runs evaluation → training and
never the other way, and `DevicePolicy` / `PrecisionPolicy` already sit on the training
side for exactly this reason. `evaluation.generation` re-exports the enum, so every
existing import resolves unchanged.

**The member values did not move**, which is why every evaluation digest is unaffected.

### 12.2 — `TrainingConfig.reasoning_policy`

A closed-set field reusing the existing enum, defaulting to `MODEL_DEFAULT`. It enters
the canonical form **only when it is not `MODEL_DEFAULT`** — the exact value-gating rule
S3G.2 established for `validation_strategy`, for the same reason.

### 12.3 — The training backend

`_encode` takes the policy and passes the mapped kwargs to **both** render calls. On this
template the full-sequence render is invariant to the keyword — measured, not assumed —
but `build_labels` refuses unless the prompt is a true prefix of the full sequence, and
rendering the two halves of that comparison under different rules is how a boundary
silently shifts. One policy, one rendering, both calls.

A run that binds a policy the template would ignore is **refused before any optimizer
step**, using the shared honour check rather than a second opinion — the D26a rule,
arriving on the training side.

### 12.4 — `chat_render_policy_hash` (§23)

`ChatRenderPolicy` binds **model-facing semantics and nothing else**: tokenizer id,
tokenizer revision, template source digest, reasoning policy, the library-level
`enable_thinking` value (`null` ≠ `false`), `add_generation_prompt`, `tokenize`, and its
own version. **No cache path, no host name, no user name, no RAM figure, no timestamp,
no output root** — D34 and D36's rule applied before the field exists rather than after a
rebuild fails. A test asserts the field list rather than trying a few paths.

Measured, for the S3M.1 preview configuration:

```
training prompt-prefix render policy (DISABLED)  8619f96c5ba84dab9afe19f8a0fcf385cb452680dd50374ba0e0b9a568490db0
evaluation prompt render policy      (DISABLED)  8619f96c5ba84dab9afe19f8a0fcf385cb452680dd50374ba0e0b9a568490db0
                                                 -> TRAIN_EVAL_RENDER_IDENTITY_PARITY: TRUE
training prompt-prefix render policy (legacy)    892e003d29a2bbc034c0d3ee6ab4208a8bd274de21dfe24804c750a9db898a55
                                                 -> differs from evaluation: TRUE
training full-sequence render policy (DISABLED)  c5e83324ce311507de4c1ed5f450c7c13647dfc893e400a853f526ad12a1c6e0
```

**Where it is recorded.** In `backend_result.json` evidence (structured block plus both
digests) and, value-gated, in the adapter manifest. `ADAPTER_MANIFEST_VERSION` is
**deliberately not moved**: the S3G.2 precedent, and the reason candidate 001's and
candidate 002's manifests still verify byte-for-byte.

**Where it is deliberately NOT computed:** at plan time. A full render identity needs the
template source digest, which needs a loaded tokenizer, and planning is a provable dry
run that imports no framework. The *plan* binds the reasoning policy through
`config_hash`; the *run* binds the render call. Both are deterministic; neither requires
weakening the planner's purity invariant.

**Not added to the evaluation runtime.** D37 is train-side parity (§20 of the brief), and
recording a new identity in evaluation artefacts would move report and manifest digests
in a milestone that is not an evaluation-policy milestone. The parity proof is a test
that constructs both sides' policies; neither production path copies from the other.

### 12.5 — Plan hyperparameters

`reasoning_policy` appears in `plan.hyperparameters` value-gated, so a plan record is
readable without opening a config document and no historical plan is re-identified.

---

## 13 — Future training plan PREVIEW (§48) — no candidate, no authority

A **neutral diagnostic** configuration, deliberately not named candidate 003, derived
from candidate 002's configuration with the reasoning policy as the only changed field.

```
REFERENCE  candidate 002    reasoning_policy model_default   config_hash 08be37d3…  (UNCHANGED)
PREVIEW    m62-s3m1-d37-parity-preview
                            reasoning_policy DISABLED        config_hash 99a893bcb05f7ace2585939273b8c1445f7fc09d68605bb53e248269b77e65c2
CONTROL    same preview, policy implicit     config_hash 68df81469d3e1f652a03af08f5b215cbfb191bb6095f3fa8c8aed11a558f7083
           canonical-body key difference: ['reasoning_policy']   -> the field is what moved it

PLAN_PREVIEW_HASH:      b850772473907db6cc80afe2b591bbd6dcfde5aa5eaf38be35b38f7583dc4cba
CONTROL_PLAN_HASH:      4cc75253d0ab48140d98bb0db877d0e221abbbdc009594f9e99606725f11b7c5
PLAN IDENTITY MOVES WITH THE POLICY:  YES
hyperparameters['reasoning_policy']:  'disabled'   (absent from the control)
PLAN_BLOCKERS:          1 — "model weights are not known to be cached and the download
                        policy is deny". Reported, not suppressed: the reviewed cache is
                        not reachable from repository authority this session, and the D30
                        fix correctly refuses to call such a plan executable.
PLAN_WARNINGS:          1 — CPU-run caution
IS_EXECUTABLE:          false
SIDE EFFECTS:           performs_training false · creates_adapter false ·
                        contacts_network false · downloads_model false
RUN DIRECTORIES:        unchanged (the two existing candidates; nothing created)
TRAIN TOKEN:            NOT DERIVED
```

A zero-blocker plan is **not** required here and was not sought: no candidate and no
corpus v3 is being designed, and the one blocker is an operator input, not a defect.

---

## 14 — Historical immutability, verified rather than asserted

| Artefact | Expected | Measured | |
|---|---|---|---|
| candidate 001 adapter manifest | `1f76ccfb…` | `1f76ccfbb8efc566c293ab6430d041dd24748035ed48aec6552d1e3bac24699f` | **unchanged** |
| candidate 002 adapter manifest | `11897e16…` | `11897e16b081cc4df2517f1c0c0904b7b7580ab4daf8fea0157e49ee4e2f6ca8` | **unchanged** |
| `verify_completed_run` (001 / 002) | 0 problems | 0 / 0 problems | **PASS** |
| candidate 001 config (this root) | `e80e04e4…` | `e80e04e485e4405c02b0037777435986a1224a4688c9d30446991fc14555c323` | **unchanged** |
| candidate 002 config | `08be37d3…` | `08be37d37dd403ea8b049ab7bb32498f5d767ef013876920783ad4669e608649` | **unchanged** |
| `gate_policy_hash` | `e5003319…` | `e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5` | **unchanged** |
| `generation_policy_hash` (S3I/S3L) | `c6b0b682…` | `c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7` | **unchanged** |
| `m62-defensive-quality-train v1` | `9bbac2f0…` | matches | **unchanged** |
| `m62-defensive-quality-train v2` | `24ceb1e0…` | matches | **unchanged** |
| `m62-defensive-eval v3` | `7c948236…` | matches | **`USED_IMMUTABLE`** |
| `m62-defensive-eval v1 / v2` | `0970600c…` / `82b60bfd…` | match | unchanged |

Both candidate manifests reload through `AdapterManifest.from_dict`, which re-derives the
digest and refuses a mismatch — so this is a re-derivation, not a file comparison.

**Backward compatibility (§45).** A config document that omits `reasoning_policy` parses
to `MODEL_DEFAULT`, which is the honest **legacy implicit template default** — pass no
keyword, let the template decide. It is deliberately **not** read as `DISABLED`: that
would retroactively claim candidate 001 and candidate 002 were fitted under a prefix they
never saw. `TRAINING_SCHEMA_VERSION` stays `m62.training_config.1`; a misspelt key is
still refused rather than silently defaulted.

---

## 15 — Classification

```
D37_REPRESENTATION_DEFECT:        YES
D37_TERMINATION_CAUSAL_CAPABLE:   YES
D37_HISTORICAL_CAUSALITY:         NOT_ESTABLISHED
```

**Why `CAUSAL_CAPABLE: YES`, and exactly how far that goes.** The definition is: the
divergence changes model-facing context/turn semantics in a way that *could plausibly*
alter when the model predicts the supervised terminator. It does, by a concrete
mechanism now measured rather than hypothesised:

* the fine-tune was taught, at the assistant-turn start, to emit
  `<think>\n\n</think>\n\n` **first**, and to emit `<|im_end|>` only after the answer that
  follows it;
* at evaluation time those 4 tokens are already in the prompt, so the model begins
  generating from a position the fine-tune **never saw as a generation start**;
* the whole learned turn shape — including where `<|im_end|>` falls relative to the
  answer — is therefore conditioned on a context that does not occur at inference.

**This is not a claim that D37 caused the S3I or S3L failures.** It cannot be: the
mismatch applies to every task family equally while the measured damage is
family-specific, and separating it from adapter capacity or family-length dominance
**requires generation**, which this milestone was not authorised to perform and did not
perform. `NOT_ESTABLISHED` is the honest status and it stays that way until a controlled
model experiment says otherwise. **Fixing D37 is not predicted to restore 9/9.**

---

## 16 — Engineering correctness, decided independently of candidate quality

Should a training run whose future eligibility evaluation declares
`reasoning_policy = DISABLED` train under the same explicit template rendering policy?

**Yes** — and the argument does not depend on whether it improves any score:

1. **A run cannot be reproduced from its own record if the record omits how it rendered.**
   Before this milestone, no training config, plan or manifest could answer *"what exact
   text did the model see?"*, and the one digest whose stated job is that question
   answers a different one.
2. **The instrument and the subject must agree.** An evaluation measures a model under a
   prefix; a fine-tune fits it under another. That is an uncontrolled variable sitting
   between every candidate and every gate.
3. **It costs the corpus nothing.** The authored rows do not move; only the loss boundary
   does, and it moves *off* template machinery and *onto* the authored answer.

The invariant now holds end to end: the training configuration binds the policy, the
rendered messages use it, `config_hash` and `plan_hash` include it, run records carry a
render-policy identity that proves the **call**, evaluation binds the same value, and a
test asserts the two identities are equal.

---

## 17 — Tests

**New file:** `jarvis/tests/test_training_gym_m62_s3m1_d37_template_parity.py` —
**76 tests, 76 passed.**

Coverage, against the brief's requirements: legacy-vs-DISABLED divergence (§29.1);
train-DISABLED equals eval-DISABLED in bytes (§29.2) and tokens (§29.3); render identity
differs implicit-vs-DISABLED (§29.4) and matches train-vs-eval (§29.5); target content
preserved (§29.6); closing brace supervised (§29.7); terminator supervised and equal to
the assistant terminator (§29.8, §32); prompt prefix masked (§29.9); no reasoning markup
supervised, and the two-sided proof that it *was* before (§29.10, §33); historical config
parses and hashes unchanged (§29.11); historical manifest not rewritten (§29.12); future
config binds the policy (§29.13); plan identity moves with the policy (§29.14); the render
identity binds no host state (§29.15, §24, §47); no held-out material in the file
(§29.16); the cross-path parity test with the comparable region defined correctly (§30);
the four `chat_render_policy_hash` property cases (§47); the honour check in both
directions; and source-level proof that both subsystems share **one** enum and **one**
honour check.

**Non-vacuity, demonstrated rather than claimed.** In a throwaway worktree at the
starting HEAD, with the shared module present and **only** the one production behaviour
reverted (the training render call stops carrying the policy):

```
23 failed, 52 passed, 1 skipped
```

The 23 are exactly the parity, supervised-span, terminator, brace, thinking-markup and
honour-check tests. The worktree was removed.

**A real-template test runs, and is not a mock.**
`test_reviewed_tokenizer_reproduces_every_claim` renders the digest-verified reviewed
template with jinja2 and asserts the whole chain — default == enabled, DISABLED == default
+ 19 chars, the full sequence invariant to the keyword, the target following the DISABLED
prompt exactly, and no `<think>` in the supervised remainder. It **passed** on this host.
It `skip`s where the reviewed template is not reachable, because no test may require an
operator-supplied artefact; it never degrades to a silent pass.

**Two pre-existing tests were updated deliberately, and both were written to require it:**

* `test_the_training_backend_binds_no_reasoning_policy_at_all` (S3M) pinned the *defect*,
  with an inverted assertion and a docstring saying *"if a future milestone binds a
  reasoning policy on the training side, this test fails — which is the point."* It now
  pins the fix. S3M's diagnosis is not revised: it correctly described `06480cb`.
* `test_the_backend_passes_the_policy_it_was_given_and_never_hard_codes_thinking`
  (S3F.2) asserted the evaluation backend held exactly one literal `True`/`False` pair —
  the honour check. That check moved to the shared module, so the assertion splits and
  gets **stricter** on the evaluation side: it must now hard-code **no** literal at all.

### Suite results

| Scope | Result |
|---|---|
| New S3M.1 file alone | **76 passed, 0 failed** |
| S3M.1 + S3M + S3F.1 + S3F.2 + S3G.2 + training execution + evaluation runner | **565 passed, 1 skipped, 0 failed** |
| **Focused M62 (`-k m62`, `--ignore=tests/test_live_brain_v61.py`)** | **2951 passed, 18 skipped, 0 failed** (2m15s) |

**2951 reconciles exactly**: S3M's 2875 + 76 new. The `--ignore` is the pre-existing
`openai` collection error PROGRESS §14.49 records, not a regression.

**D39 was not triggered and not fixed.** The authoritative `-k m62` collection is
alphabetical, which is why it is clean; nothing here changes either file involved.

### Static and security gates

| Gate | Result |
|---|---|
| `git diff --check` | **PASS** |
| `compileall` over every changed/new file | **PASS** |
| Ruff | **NOT RUN — absent from this host**, reported rather than silently skipped |
| Bandit | **NOT RUN — absent from this host** |
| Secret scan (`core.redaction_policy.scan_for_leaks`) over the changeset | **PASS** — findings named below, none suppressed |
| Host-path scan | **PASS** — no new host path; the only `/home/` hits are pre-existing synthetic scanner fixtures in the S3F.2 file, byte-identical to HEAD |
| `TRAIN:` / `EVAL:` token literal scan | **PASS** — none |
| Runtime artefact exclusion | **PASS** — nothing under an ignored root is tracked |

The scanner reports `reasoning` on five files. Every hit is the literal `<think` — in
prose describing the template's output, in the shared module's docstring, or in the
synthetic test constant. Operator ruling **H4** classifies reasoning markup as hygiene,
not a security leak, exactly as S3G, S3G.2, S3H, S3J and S3M recorded. The `home_path`
and `secret` findings on the S3F.2 test file are **present at HEAD too**, verified by
scanning both versions; S3M.1 added none.

---

## 18 — Source change accounting

| Kind | Files |
|---|---|
| **Production (new)** | `training_gym/training/chat_render.py` |
| **Production (changed)** | `training_gym/training/config.py`, `training_gym/training/backends/transformers_peft.py`, `training_gym/training/artifacts.py`, `training_gym/training/execution.py`, `training_gym/training/planner.py`, `training_gym/evaluation/generation.py`, `training_gym/evaluation/backends/transformers_peft.py` |
| **Tests (new)** | `tests/test_training_gym_m62_s3m1_d37_template_parity.py` |
| **Tests (changed)** | `tests/test_training_gym_m62_s3m_structured_output_diagnosis.py`, `tests/test_training_gym_m62_s3f2_reasoning_policy.py` |
| **Docs** | this file, `PROGRESS.md` |

The two evaluation-side changes are the enum's import and the honour check's delegation.
**No evaluation semantics, policy, threshold, grader, gate, parser or budget changed**,
and both sealed evaluation digests re-derive byte-identically.

---

## 19 — Limitations

1. **No model has been generated under the fixed rendering.** Every claim here is about
   representation. Whether it changes any measured behaviour is **unknown, not
   estimated**, and requires a controlled experiment.
2. **D37's historical causal weight stays `NOT_ESTABLISHED`.** Candidate 001 and
   candidate 002 are not reinterpreted.
3. **The reviewed model cache was not reachable from repository authority**, so the
   tokenizer used was reconstructed from the repository's own quarantined runtime
   artefact and admitted only because its template digest matched `a55ee1b1…` exactly.
   The weights were never loaded and were not needed.
4. **The parity proof is bound to this template at this digest.** Any change to the
   tokenizer revision or the template invalidates §5–§9 and it must be re-measured — the
   same rule PROGRESS §14.30 already applies to the 512 qualification.
5. **Six synthetic fixtures, two production message shapes.** Multi-turn is out of scope
   because production conversion does not accept it.
6. **The plan preview carries one blocker** (unverified cache) and is deliberately not
   executable. It proves the binding, not readiness.
7. **`ChatRenderPolicy` is recorded on the training side only.** Evaluation's rendering
   was already correct and its artefacts were deliberately left unmoved.

---

## 20 — Final status

```
S3M1_D37_TEMPLATE_PARITY:         PASS
D37_STATUS:                       FIXED
D38_STATUS:                       OPEN_UNCHANGED
D39_STATUS:                       OPEN_UNCHANGED

TRAIN_RENDER_REASONING_POLICY_BEFORE:  IMPLICIT_TEMPLATE_DEFAULT (MODEL_DEFAULT)
TRAIN_RENDER_REASONING_POLICY_AFTER:   BOUND BY CONFIG; DISABLED for train/eval parity
EVAL_RENDER_REASONING_POLICY:          DISABLED (unchanged, still explicit)

CANDIDATE_001 / CANDIDATE_002:    EVALUATED_NOT_ELIGIBLE, both unchanged
CANDIDATE_003_CREATED:            NO
EVAL_V4_CREATED:                  NO
EVAL_V4_REQUIRED_BEFORE_CANDIDATE_003:  YES
```

---

## 21 — Exact NEXT

**S3M.1 is closed and authorises nothing further.** D37 is fixed as an engineering
correctness property, not as a candidate.

The next step is a **separate operator decision about the first controlled future
candidate experiment**, and it has prerequisites that are not details:

1. **Freeze a fresh `m62-defensive-eval v4` before any training.** `eval-v3` is used.
2. **Choose exactly ONE experimental axis.** D37 closure is now baked into any future
   run that binds the policy — which means a candidate that binds it *and* changes
   another dial has moved two variables. The two candidate axes S3M identified remain
   `ATTENTION_ONLY` (S3M option B) and "D37 closure as the primary variable" (option C).
   **They cannot be combined.**
3. **Note the comparability cost honestly.** A future candidate that binds `DISABLED`
   is not directly comparable to candidate 001 or 002, which were fitted under the
   template default. Each candidate is comparable to its **own** simultaneously-measured
   baseline under identical policy digests, which is what every gate already does.
4. **A fresh `TRAIN` authority and a fresh single-use `EVAL` authority**, at a new
   generation. S3M.1 created neither.

**Do not** raise `max_new_tokens`, add structured rows, change gates, graders, thresholds
or the refusal detector, or fix D38/D39 as riders on that candidate.
