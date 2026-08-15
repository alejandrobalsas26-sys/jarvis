# V69 M62 S3M.2 — D38 output-budget exhaustion instrumentation

> **Status: `D38_STATUS: FIXED`, as an OBSERVABILITY defect.** Output-budget exhaustion is
> now a first-class body-free diagnostic metric, derived entirely from termination
> metadata the artefacts already carried. **No gate reads it. No generation behaviour
> changed. `max_new_tokens` is still 512.**
> **Zero training, zero evaluation, zero model generations, zero optimizer steps, no
> `TRAIN` or `EVAL` authority, no candidate 003, no `eval-v4`.**

| | |
|---|---|
| Milestone | V69 M62 **S3M.2** — D38 only |
| Date | 2026-08-15 |
| Branch | `jarvis-v69-m62-training-gym` |
| Starting HEAD | `475f3c9a4a60519d0a59497bbfb66b4050800a3e` |
| Master | `3705114228edef2f665be349c5c4429b7b16777a` (untouched) |
| Preceding milestones | S3M — `V69_M62_S3M_STRUCTURED_OUTPUT_DIAGNOSIS.md` · S3M.1 — `V69_M62_S3M1_D37_TEMPLATE_PARITY_QUALIFICATION.md` |

---

## 1 — Authorisation and boundary

The operator authorised **D38 as its own milestone**, to close it as an **observability**
defect: make output-budget exhaustion explicit and measurable from already-existing
body-free termination metadata, and **do not turn it into an eligibility gate**.

Measured rather than asserted:

```
TRAIN_TOKEN_CREATED / CONSUMED:        NO / NO
EVAL_TOKEN_CREATED / CONSUMED:         NO / NO
MODEL_GENERATIONS:                     0
MODEL_RESPONSE_TOKENS_GENERATED:       0
OPTIMIZER_STEPS:                       0
ADAPTERS_CREATED / MUTATED:            0 / 0
MODEL_WEIGHTS_LOADED:                  NO   (no tokenizer either)
CANDIDATE_003_CREATED / EVAL_V4:       NO / NO
HELD_OUT_PROMPT_OR_RESPONSE_BODIES_READ: NO
RAW_RESPONSE_BODIES_READ / PERSISTED:  NO / NO
D37_STATUS:                            FIXED_UNCHANGED
D39_STATUS:                            OPEN_UNCHANGED
GATES / GRADERS / THRESHOLDS:          UNCHANGED  (`e5003319…` re-derived, zero drift)
GENERATION POLICY:                     UNCHANGED  (`c6b0b682…` re-derived byte-identical)
```

**Sealed and not reopened:** the S3I verdict, the S3L verdict, candidate 001, candidate
002, D28, D29, D33, D34, D35, D36, D37 and S3M's diagnosis. Both candidates remain
`EVALUATED_NOT_ELIGIBLE`, measured under the instrument actually used, and nothing here
rescores, reinterprets or retroactively invalidates either.

---

## 2 — D38, stated exactly

The instrument exposed two names for one thing and no name for the other:

| Signal | Where it lived | What it meant |
|---|---|---|
| `EvaluationResult.input_truncated` | set by the backend as `input_tokens >= policy.max_input_tokens` | the **PROMPT** was clipped before generation started |
| `ArmScore.truncated` | `scoring.py` — `truncated=result.input_truncated` | the same thing, carried to the score |
| `metrics.truncation_rate` | `metric("truncation_rate", lambda s: s.truncated)` | the same thing, as a rate. **This is what OG-3 reads** |
| **output-budget exhaustion** | `EvaluationResult.finish_reason == MAX_NEW_TOKENS` | **no metric, no aggregate, no gate** |

So OG-3's `truncation 0/9` was **correct** and was about the prompt, while in S3L the
candidate ended at the ceiling on **5 of 36** tasks and both structured-output failures
were among them. The single most diagnostic fact about that run was present in the
artefacts and absent from every number.

**This was never an OG-3 bug.** OG-3 measured the thing it was implemented to measure.
The defect is that there was no first-class metric for the *other* thing.

---

## 3 — Source-level flow matrix

Traced end to end before anything was edited.

| Stage | Location | Carries the termination state? |
|---|---|---|
| backend generation | `evaluation/backends/transformers_peft.py` `generate` | produces it |
| finish determination | same file, one line: `MAX_NEW_TOKENS if output_tokens >= policy.max_new_tokens else END_OF_SEQUENCE` | **the origin** |
| generation result | `evaluation/backend.py` `EvaluationResult.finish_reason` | yes, typed |
| result persistence | `EvaluationResult.to_dict()` → `*-results.jsonl` | **yes, body-free, already** |
| ArmScore construction | `evaluation/scoring.py` `score_arm` | **was: NO** → now: `output_budget_exhausted` |
| metrics | `evaluation/metrics.py` `build_arm_metrics` | **was: NO** → now: rate + count + per-family |
| per-family aggregation | `Metric.by_family` | inherited automatically |
| paired comparison | `evaluation/comparison.py` | **was: NO** → now: a diagnostic matrix |
| report | `evaluation/reports.py` `EvaluationReport.to_dict()` | via `operational_summary` + the paired key |
| gate input | `evaluation/gates.py` | **NO, before and after — by design** |
| body-free persistence | `evaluation/score_evidence.py` | **was: NO** → now: one allowlisted field |

---

## 4 — The finish-reason closed set

Read from source, not assumed. `FinishReason` has exactly six members.

| State | Produced by | Complete? | Error? | Output-budget exhaustion? | Body-free? | Persisted? | Was it counted? | Read by a gate? |
|---|---|---|---|---|---|---|---|---|
| `stop_sequence` | **nobody** — `stop_sequences` are applied as post-hoc text truncation, never as a stopping criterion | yes | no | **no** | yes | yes | no | no |
| `end_of_sequence` | production + fake | yes | no | **no** | yes | yes | no | no |
| `max_new_tokens` | production (`output_tokens >= policy.max_new_tokens`) | no | no | **YES** | yes | yes | **no** | no |
| `timeout` | fake backend only — D33 means the timeout is declared and never enforced in production | no | no (a *different* event) | **no** | yes | yes | no | no |
| `error` | production `_failed`, `blocked_result`, the runner | no | **yes** | **UNMEASURED** | yes | yes | no | no |
| `unknown` | the dataclass default | unknown | unknown | **UNMEASURED** | yes | yes | no | no |

**Fail-closed on an unknown state.** The classification table is exhaustive over the enum
and a member absent from it **raises** rather than defaulting. An arbitrary *string* was
already impossible: `EvaluationResult.__post_init__` coerces through `FinishReason(...)`.

---

## 5 — The semantic predicate

```
FinishReason.output_budget_exhausted        -> bool | None   (the one authority)
EvaluationResult.output_budget_exhausted    -> bool | None   (adds the status guard)
```

Three-valued, following the exact rule `ArmScore.schema_valid` already uses: **`None` is
UNMEASURED and is never an optimistic `False`.** A result that produced no output at all —
failed, blocked, timed out, interrupted, unsupported — is unmeasured for this property and
leaves the denominator, rather than being counted as a clean self-terminated completion.

**Derived from the explicit termination reason, never from token arithmetic.** Token-count
equality is used only as a *consistency assertion* (§7), which is the direction the brief
required: the finish reason is the authority and the count checks it.

---

## 6 — Three events, kept apart

| Field | Event | Governed by |
|---|---|---|
| `input_truncated` / `ArmScore.truncated` / `truncation_rate` | the **prompt** was clipped before generation | unchanged, and OG-3's subject |
| `output_budget_exhausted` / `output_budget_exhaustion_rate` | the **response** consumed `max_new_tokens` | **D38, new** |
| `timed_out` / `FinishReason.TIMEOUT` / `timeout_rate` | wall-clock timeout | **D33, untouched** |

D38 does not absorb D33 and does not redefine input truncation. All three can be reported
in the same run, and a fixture proves input truncation and output exhaustion can be true
simultaneously.

---

## 7 — The consistency check (`output_budget_consistency_problems`)

The relationship was **discovered from source**, not assumed: the production backend uses
`>=`, not `==`, so a backend that appends a terminator inside the budget still classifies
correctly.

It **returns problems rather than raising in production**, deliberately: the backend that
owns the comparison cannot check itself with it without being circular. What it is for is
a *foreign* result — a replayed record, a second backend — where the two facts could
genuinely disagree. A disagreement is **reported**, never silently relabelled, and a
backend that did not count tokens (`output_tokens < 0`) produces no finding rather than a
false one.

**Run over all 144 sealed historical generations: 0 mismatches.**

---

## 8 — What was implemented

**Seven production files changed, none added.** No unrelated cleanup.

### 8.1 `evaluation/backend.py` — the single authority (§31)

`FinishReason.output_budget_exhausted`, its exhaustive classification table,
`EvaluationResult.output_budget_exhausted`, and `output_budget_consistency_problems`.
A test asserts that `scoring`, `metrics`, `comparison` and `reports` contain **no**
`MAX_NEW_TOKENS` literal at all — the comparison lives in the table that defines it and in
the backend that produces it, and nowhere else.

### 8.2 `evaluation/scoring.py` — the body-free score field

`ArmScore.output_budget_exhausted: bool | None`, carried from the result on **both**
construction paths (the not-ok path and the scored path), validated as a true tri-state so
a truthy string cannot aggregate as an exhausted generation.

`ArmScore.truncated` is **unchanged**, and a test pins the literal
`truncated=result.input_truncated` in `score_arm`'s source.

`SCORING_VERSION` → **`m62.evaluation_scoring.5`**, following the D24/D26/D27 precedent
that an `ArmScore` shape change moves it.

### 8.3 `evaluation/policy.py` — the metric set becomes part of the metric policy

This is the identity half of the fix, and it is a real defect in its own right.

Before: `MetricPolicy.to_dict()` bound only *how* a number may be reported — the
denominator rules and the p95 sample floor — and **nothing about which numbers exist**. So
an instrument could gain or lose a canonical metric while `metric_policy_hash` stayed
byte-identical, and two reports claiming one metric-policy identity could be measuring
different things.

After: `CANONICAL_METRIC_NAMES` (the declared metric set) and `METRIC_SET_VERSION` live in
`policy.py` and are inside `MetricPolicy.to_dict()`. `metrics.build_arm_metrics` asserts
that what it emits equals the declaration **in both directions** and raises otherwise, so a
metric added to the computation and not declared — which is how an instrument changes
without its identity moving — is refused before a number is published.

`METRICS_VERSION` is now defined once, in `policy.py`, and re-exported by `metrics.py`.
Two constants that could disagree would be one more place for the instrument and its
identity to drift apart.

### 8.4 `evaluation/metrics.py` — the metric

| Key (in `operational`) | What it is |
|---|---|
| `truncation_rate` | **legacy name, unchanged meaning** — input truncation. Not deleted. |
| `input_truncation_rate` | the *same `Metric` object*, renamed via `dataclasses.replace`. One authority, so the two cannot drift; a test asserts they differ by **name only**. |
| `output_budget_exhaustion_rate` | the D38 rate, with `numerator`, `denominator`, `excluded`, `missing`, `by_family` and `by_split` |
| `output_budget_exhaustion_count` | the absolute count, taken straight off that rate's own `numerator`, with the exhausted task ids (body-free, capped at 20, exactly as the security counts already publish them) |

### 8.5 `evaluation/comparison.py` — the paired diagnostic

`output_budget_exhaustion_matrix(comparisons)`, built in the same shape as the existing
`refusal_counts`: a pure function over already-scored comparisons, body-free, counted
rather than averaged. Cells: `both_exhausted`, `candidate_only`, `baseline_only`,
`neither_exhausted`, and `unmeasured` as **its own bucket** — "this arm errored" and "this
arm finished inside its budget" are different facts.

It carries `is_a_gate: False` and states its own limitations. **No sign test, no bootstrap,
no interval, no PASS/FAIL**, and a test asserts the absence of every such key.

### 8.6 `evaluation/reports.py` — the report surface

One new key, `output_budget_exhaustion_paired`. The per-arm counts, rates and per-family
breakdown already travel in `operational_summary` beside every other operational metric;
duplicating them into a second block of a hash-bound document would create two numbers to
keep in step. What is genuinely new is the **paired** view, so that alone was added.

### 8.7 `evaluation/score_evidence.py` — body-free persistence

`output_budget_exhausted` added to the closed `SCORE_EVIDENCE_FIELDS` allowlist and to the
record. It is **not** coerced with `bool()`: `bool(None)` would publish an errored arm as a
clean, non-exhausted completion.

`SCORE_EVIDENCE_VERSION` → `m62.evaluation_score_evidence.2`. **Historical `.1` records
still read**, because the field list is an *allowlist*: a record that omits the new key is
accepted and one that carries an undeclared key is refused. Pinned by a test.

---

## 9 — Denominator, and exactly how errors affect it

```
denominator = generations that produced output and whose termination state is classified
numerator   = of those, the ones whose finish reason is MAX_NEW_TOKENS
excluded    = generations with no decidable termination state (errored, blocked,
              timed out, interrupted, unsupported, or an unclassified state)
missing     = tasks in the frozen pack that produced no score for this arm
```

For a normal completed 36-task arm with 0 errors the denominator is **36**, which is what
both sealed runs produce.

**Errors leave the denominator rather than counting as non-exhausted**, because counting
them as `False` is precisely "counting an error as a clean non-exhausted completion". The
exclusion is never silent: `Metric.excluded` carries the count and the metric gains an
explicit limitation string naming it.

The denominator does **not** depend on whether the output was parseable. Output exhaustion
is a generation property, not a structured-output property.

---

## 10 — Gate non-interference

```
OUTPUT_BUDGET_EXHAUSTION_GATE_REFERENCES:  0
GATE_LOGIC_CHANGED:                        NO
GATE_POLICY_IDENTITY_CHANGED:              NO
GATE_POLICY_IDENTITY_CHANGED_TRANSITIVELY: NO
OG3_CHANGED:                               NO
SECURITY_VETO_ADDED:                       NO
```

`GatePolicy().policy_hash()` re-derives **`e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5`** — byte-identical to the value S3G predeclared and S3I, S3J, S3J.1, S3K, S3L and S3M.1 all reproduced. It did not move even transitively, and the structural reason is checkable rather than asserted: **`GatePolicy.to_dict()` does not serialise the metric policy at all**. A test pins its exact key set.

`gates.py` contains zero references to `output_budget_exhausted`,
`output_budget_exhaustion_rate`, `output_budget_exhaustion_count` or `finish_reason`,
before and after — asserted over the module source.

**OG-3 remains bound to input truncation**, its historical semantics. It was not renamed;
`truncation_rate` still exists with the same meaning, so no historical report is
reinterpreted. The `input_truncation_rate` alias exists so that *future* documentation can
be unambiguous without touching the past.

---

## 11 — Policy identity: exactly which identities move, and why

| Identity | Before | After | Moves? |
|---|---|---|---|
| `metric_policy_hash` | `2d0830103bc11f280fc2a25e5ac8f0f79bd3e6a1ad589046d238e9fc5d9cfd87` | **`e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a`** | **YES — required** |
| `gate_policy_hash` | `e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5` | same | no |
| `generation_policy_hash` | `c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7` | same | no |
| `grader_policy_hash` | `2059579278f42d159447b3f281df2fa5b34e058d03cf944f7f0b8547763447b2` | same | no |
| `statistical_policy_hash` | `663ebf65b73536fe3bd41043568a1f737ff751a43a964d14ff67c4e12662bf18` | same | no |
| `family_policy_hash` | `580fbe9104cbe684f702cba016e1191c83745fb8502642636c3fc885135065b1` | same | no |
| `resource_policy_hash` | `0486300a3bca61717b0dd119721915709a4f34dd403f5ecdd45eb209bef65834` | same | no |

**The exact canonical delta**, and nothing else:

```
+ "metric_set_version": "m62.evaluation_metrics.2"
+ "canonical_metrics": { quality: [...], security: [...], refusal: [...],
                         operational: [... input_truncation_rate,
                                       output_budget_exhaustion_count,
                                       output_budget_exhaustion_rate ...] }
```

**A future evaluation plan and report move with it**, because both bind
`policies.metrics.policy_hash()` — which is the point: a plan that did *not* move would be
claiming an instrument identity that no longer describes the instrument.

**The consequence, stated rather than hidden.** The two sealed evaluation *config
documents* are byte-unchanged on disk, but re-read under the new instrument they now hash
differently:

| Config | `evaluation_config_hash` as sealed | re-derived now |
|---|---|---|
| `m62-s3i-quality-heldout-live` | `cf9ca9bd…` | `c9449f1d…` |
| `m62-s3l-quality-heldout-live` | `3d7725d3…` | `c16c5257…` |

That is the honest, intended consequence of §16: a configuration re-read by a different
instrument describes a different measurement. It is the same shape S3M.1 recorded for
candidate 001's config hash. **It changes nothing about the sealed artefacts** — no
verifier re-derives a historical config hash, and §13 shows both generations still verify
with 0 problems.

The alternative — exempting the metric set from identity to preserve `2d083010…` — was
explicitly refused. An identity kept alive by leaving the changed part out of it is a lie
with a hash attached.

---

## 12 — Retrospective validation (no rescore)

Read through the **production authority** from body-free termination metadata: `task_id`,
`role`, `status`, `finish_reason`, `output_tokens`, `input_truncated`, `timed_out`. The
result-record key list was enumerated **before** anything was read, and it carries
`response_sha256` and `response_chars` and **no response-bearing field**. `task-pack.jsonl`
— which holds the held-out prompts and targets — was **not opened**.

**No ArmScore was rebuilt, no gate ran, no eligibility was derived, nothing was written.**

| | expected (S3M record) | **measured from disk** |
|---|---|---|
| S3I baseline | 0 / 36 | **0 / 36** = 0.000000 |
| S3I candidate 001 | 1 / 36 | **1 / 36** = 0.027778 |
| S3L baseline | 0 / 36 | **0 / 36** = 0.000000 |
| S3L candidate 002 | 5 / 36 | **5 / 36** = 0.138889 |
| baseline `end_of_sequence` | 72 / 72 | **72 / 72** |
| input truncation, all four arms | 0 | **0 / 144** |
| `ArmScore.truncated`, all four arms | 0 | **0** |
| errors / timeouts / unmeasured | 0 | **0** |
| token-count consistency mismatches | — | **0 of 144** |

Every figure reproduces exactly. **No discrepancy to investigate.**

### 12.1 Structured-family correlation (body-free)

Ids, family, finish reason and the parse verdict. No prompt, no target, no response, no
decoded text, and no reinterpretation of any historical gate.

| run | task | family | finish reason | exhausted | `json_parseable` | `schema_valid` |
|---|---|---|---|---|---|---|
| S3I | `adv-report-03` | `structured_report` | `max_new_tokens` | **yes** | False | False |
| S3I | `he-report-04` | `structured_report` | `end_of_sequence` | no | False | False |
| S3L | `he3-report-01` | `structured_report` | `max_new_tokens` | **yes** | False | False |
| S3L | `he3-report-04` | `structured_report` | `max_new_tokens` | **yes** | False | False |
| S3L | `he3-evidence-01` | `evidence_request` | `max_new_tokens` | **yes** | n/a | n/a |
| S3L | `he3-evidence-03` | `evidence_request` | `max_new_tokens` | **yes** | n/a | n/a |
| S3L | `adv3-tool-01` | `tool_call_schema` | `max_new_tokens` | **yes** | n/a | n/a |

All other structured generations on both arms of both runs ended `end_of_sequence` and
parsed. **No baseline structured generation reached the ceiling in either run.**

This reproduces S3M's finding exactly and adds nothing to it: three of the four structured
failures ran to the ceiling, and the three non-structured ceiling endings **passed** their
graders — which is the concrete reason exhaustion must not be a gate.

### 12.2 No retroactive gate

**There is no D38 gate, and none is implied.** Candidate 001 and candidate 002 are
`EVALUATED_NOT_ELIGIBLE` for the reasons S3I and S3L recorded, under `e5003319…`, and
nothing above changes, adds to or reweighs a single one of them. The numbers in §12 are a
**historical diagnostic metric value** and nothing else. Neither candidate "would fail a
D38 gate", because there is no such gate to fail.

---

## 13 — Historical immutability, verified rather than asserted

| Artefact | Result |
|---|---|
| `verify_evaluation_generation` — S3I gen-1 | **0 problems** |
| `verify_evaluation_generation` — S3L gen-1 | **0 problems** |
| `verify_evaluation_generation` — smoke eval gen-2, gen-3 | **0 problems** |
| smoke eval gen-1 | 9 problems — **identical at HEAD**, verified by stashing; it is the abandoned S3E generation with an empty directory, and has nothing to do with D38 |
| S3I / S3L `report_hash` | `7f7835b8…` / `0e6351f4…` unchanged; both re-derive from their stored payloads |
| S3I / S3L recorded `metric_policy_hash` | `2d083010…` in both — **the OLD instrument's identity, preserved** |
| S3I / S3L recorded `gate_policy_hash` | `e5003319…` in both |
| S3I / S3L `eligibility` | `not_eligible` in both, unchanged |
| Candidate adapters, dataset manifests, eval-v1/v2/v3 | **not read for mutation, not rebuilt, not modified** |

**Nothing on disk was rewritten.** The generation verifier works from stored bytes — file
digests, the tree hash, cross-checks between artefacts, and a report hash recomputed over
the stored payload — so it does not consult a live policy object and a moved
`metric_policy_hash` cannot invalidate a sealed generation.

---

## 14 — Predeclared closure criteria

Every criterion was evaluated **before** production was edited; C13 was completed
afterwards, by construction.

| | Criterion | Result | Evidence |
|---|---|---|---|
| **C1** | current `ArmScore.truncated` / `input_truncated` semantics reproduced | **PASS** | §2; `truncated=result.input_truncated` pinned in source |
| **C2** | canonical output-budget termination state identified unambiguously | **PASS** | §4 — `FinishReason.MAX_NEW_TOKENS`, one origin line |
| **C3** | the metric derives exclusively from existing body-free termination evidence | **PASS** | §5; nothing new is generated or stored |
| **C4** | input-truncation semantics unchanged | **PASS** | §6, §10; `truncation_rate` untouched |
| **C5** | timeout semantics separate | **PASS** | §6; D33 untouched |
| **C6** | applies symmetrically to baseline and candidate | **PASS** | one `build_arm_metrics` path, no role branch; pinned by test |
| **C7** | no gate reads the new metric | **PASS** | §10 — 0 references, asserted over `gates.py` source |
| **C8** | generation configuration/behaviour identity-equivalent | **PASS** | `c6b0b682…` re-derived byte-identical from both sealed configs; the backend's finish line unchanged |
| **C9** | historical artefacts immutable, verdicts unchanged | **PASS** | §13 |
| **C10** | body-free retrospective reproduces the historical counts from disk | **PASS** | §12 — exact, no discrepancy |
| **C11** | unknown/error states fail closed | **PASS** | §4, §5 — tri-state; an unclassified member raises |
| **C12** | metric-policy / future-plan identity moves where required | **PASS** | §11 |
| **C13** | non-vacuous tests fail against the pre-fix absence | **PASS** | §15 — 8 of 64 fail under three targeted reverts |
| **C14** | no TRAIN/EVAL authority, no model generation | **PASS** | §1 |

**All fourteen passed, so the instrumentation was implemented.**

---

## 15 — Tests

**New file:** `jarvis/tests/test_training_gym_m62_s3m2_d38_output_budget.py` —
**64 tests, 64 passed.**

Coverage against the brief's list: legacy `truncated` still input truncation (§43.1);
normal EOS false (§43.2); the canonical ceiling true (§43.3); input truncation alone does
not imply output exhaustion (§43.4); both true simultaneously (§43.5); timeout ≠ output
exhaustion, in both halves (§43.6); an error is not a clean non-exhausted completion
(§43.7); unknown fails closed and an unclassified enum member raises (§43.8); per-arm count
(§43.9), rate (§43.10) and denominator (§43.11); per-family breakdown (§43.12); the paired
matrix (§43.13); arm symmetry (§43.14); zero gate references (§43.15); OG-3 still input
truncation (§43.16); generation policy and the backend's finish line unchanged (§43.17);
metric-policy identity moves (§43.18); it binds no host state (§43.19); the body-free
report and evidence contain no response (§43.20); historical artefacts deserialize and
verify unchanged (§43.21); S3I and S3L reproduce from body-free finish reasons (§43.22,
§43.23); the D37 render authority untouched (§43.24, §34); and the file reads only
body-free artefacts (§43.25). Plus the four consistency-check cases (§24), the
single-authority source assertion (§31), the legacy/alias no-drift proof (§22), and the
metric-inventory fail-closed control in both directions (§48).

**Non-vacuity, demonstrated rather than claimed.** In a throwaway worktree at the starting
HEAD with the full production diff applied and the sealed body-free generations copied in
read-only, **three targeted single-behaviour reverts** were applied:

| Revert | What it restores | Failures |
|---|---|---|
| **A** — `MAX_NEW_TOKENS` classified `False` | the pre-fix world where the ceiling is invisible | **5** — including both sealed-run retrospectives |
| **B** — `MetricPolicy.to_dict()` drops the metric-set binding | the pre-fix identity defect | **2** — and `metric_policy_hash` returns to exactly `2d083010…` |
| **C** — `score_arm` stops carrying the verdict | the plumbing gap | **1** |
| **all three together** | | **8 failed, 56 passed** |

The three failure sets are disjoint. The control run in the same worktree with the fix
intact is **64 passed**. The worktree was removed and `git worktree prune` is clean.

**One pre-existing test was updated deliberately**, and its assertion was **not** weakened:
S3M's `test_arm_score_truncated_reports_INPUT_truncation_not_output_budget` still pins the
literal `truncated=result.input_truncated`. Only its docstring changed, to record that D38
was closed by *adding* a sibling field rather than by re-pointing this one — and that the
signal is still read by no gate.

### Suite results

| Scope | Result |
|---|---|
| New S3M.2 file alone | **64 passed, 0 failed** |
| Adjacent: evaluation metrics · config · artifacts · execution · S3F.1 · S3F.2 review evidence · S3M | **421 passed, 0 failed** (23 s) |
| **Focused M62 (`-k m62`, `--ignore=tests/test_live_brain_v61.py`)** | **3015 passed, 18 skipped, 0 failed** (2m15s) |

**3015 reconciles exactly:** S3M.1's 2951 + 64 new.

**D39 was not triggered and not fixed.** The authoritative `-k m62` collection is
alphabetical, which is why it is clean; nothing here changes either file involved.

### Static and security gates

| Gate | Result |
|---|---|
| `git diff --check` | **PASS** |
| `compileall` over every changed/new file | **PASS** |
| **Ruff** | **NOT RUN — absent from this host**, reported rather than silently skipped |
| **Bandit** | **NOT RUN — absent from this host** |
| Secret scan (`core.redaction_policy.scan_for_leaks`) over the changeset | **PASS** — findings named below, none suppressed |
| Host-path scan | **PASS** — 0 added lines carry a host path; the one `/home` string in the new test file is inside the assertion that the metric-policy identity contains **no** host path |
| `TRAIN:` / `EVAL:` token literal scan | **PASS** — none |
| Runtime artefact exclusion | **PASS** — `git check-ignore` confirms `evaluation/evaluations/` and `training_runs/`; nothing runtime is tracked |
| **Body-free audit** of every new persisted/report field | **PASS** — `RAW_RESPONSE_BODIES: 0`, `PROMPT_BODIES_ADDED: 0`, `TARGET_BODIES_ADDED: 0`; 0 keys matching any body-shaped name; longest string in the new payloads is a 157-character limitation sentence written here |

The scanner reports `reasoning` on the S3M test file and `home_path` + `reasoning` on
`scoring.py`. **Both are byte-identically present at HEAD** — verified by scanning the HEAD
blobs — so S3M.2 added neither. They are the pre-existing `<think` literal (operator ruling
**H4**: hygiene, not a leak) and `scoring._PRIVATE_PATH_RE`, the detector's own pattern.

---

## 16 — Future plan preview

```
FUTURE_PLAN_PREVIEW: NOT_APPLICABLE_UNTIL_FUTURE_CANDIDATE
```

A real `EvaluationPlan` binds a **verified adapter reference** and a fresh generation
directory. Building one would mean either inventing a fake adapter — refused — or
constructing an evaluation-authority path against candidate 002 and the already-spent
`eval-v3`, which is not what an instrumentation milestone should do. Rather than weaken
plan validation to produce a preview, the binding was proved directly and by measurement
instead (§11): the plan and report both derive `metric_policy_hash` from
`policies.metrics.policy_hash()`, that value moved, and the two sealed config documents
now re-derive different `evaluation_config_hash` values from byte-identical bytes.

---

## 17 — Limitations

1. **No model has been generated under the new instrument.** Every figure here is either
   synthetic or a re-reading of sealed historical metadata. What a *future* run's
   output-budget metric will say is **unknown, not estimated**.
2. **The retrospective is 144 generations, 2 candidates, 1 base model, 1 host, 1 seed**,
   over 36-task single-author holdouts. It inherits every S3M limitation unchanged.
3. **The metric describes termination, not quality.** A response that reaches the ceiling
   may still be graded correct — S3M measured three that were — and a response that
   terminates cleanly may still be wrong. `OUTPUT_BUDGET_EXHAUSTION != FAILURE`.
4. **D38 says nothing about *why* a fine-tune stops terminating.** S3M's
   `ROOT_CAUSE_CONFIDENCE: HIGH for the mechanism · LOW for its upstream cause` is
   unchanged, and separating the candidate explanations still requires generation.
5. **`FinishReason.STOP_SEQUENCE` is unreachable in production.** `stop_sequences` are
   applied as post-hoc text truncation after generation, so that state is classified and
   tested but never produced. Its classification is a contract, not a measurement.
6. **`FinishReason.TIMEOUT` is unreachable in production too**, because D33 means the
   declared timeout is never enforced. The separation between D33 and D38 is therefore a
   design property held by tests, not one a live run has exercised.
7. **The consistency check has never found a real mismatch**, because the only backend that
   produces the finish reason also computes the comparison. It is qualified against 144
   sealed records and against synthetic disagreements, not against a second backend.
8. **The `input_truncation_rate` alias is additive and the legacy key stays.** Nothing was
   migrated, and no historical reader has to change.

---

## 18 — What future sessions must NOT do

- **DO NOT** reinterpret OG-3, `truncation_rate` or `ArmScore.truncated` as output
  truncation. They are input truncation, they always were, and a historical report that
  says `truncation 0/9` is correct.
- **DO NOT** delete `truncation_rate` in favour of `input_truncation_rate`. The alias is
  additive; both are one `Metric` object and removing the legacy key re-identifies every
  reader.
- **DO NOT** turn D38 into a gate, a veto, or an eligibility threshold without a **separate
  operator decision that designs one**. S3M.2 deliberately designs none, and §12.2 is the
  reason: exhaustion is not failure.
- **DO NOT** raise `max_new_tokens` to improve the D38 number. That moves the instrument
  for both arms, confounds the next comparison and hides the finding.
- **DO NOT** rescore S3I or S3L under the new metric, or recompute their eligibility. The
  §12 figures are a diagnostic reading of sealed metadata, not a re-run.
- **DO NOT** say candidate 001 or candidate 002 "would fail a D38 gate". There is no gate.
- **DO NOT** classify a new `FinishReason` by adding it to the enum alone. The
  classification table is exhaustive and will raise; decide what the state means.
- **DO NOT** re-implement `finish_reason == MAX_NEW_TOKENS` anywhere. There is one
  authority and a test asserts the other four modules contain no such literal.
- **DO NOT** coerce `output_budget_exhausted` with `bool()`. `None` is UNMEASURED, and
  `bool(None)` publishes an errored arm as a clean completion.
- **DO NOT** add a metric to `build_arm_metrics` without declaring it in
  `CANONICAL_METRIC_NAMES`. The run will refuse, and that refusal is the point.
- **DO NOT** restore `metric_policy_hash` to `2d083010…`. That value describes the old
  metric set and belongs to the sealed S3I and S3L records.
- **DO NOT** fix **D39** as a rider, and do not reopen **D37**, **D33**, **D29** or **D28**.

---

## 19 — Final status

```
S3M2_D38_OUTPUT_BUDGET_INSTRUMENTATION:  PASS
D38_STATUS:                              FIXED
D37_STATUS:                              FIXED_UNCHANGED
D39_STATUS:                              OPEN_UNCHANGED

NEW_OUTPUT_BUDGET_FIELD:        ArmScore.output_budget_exhausted (bool | None)
NEW_CANONICAL_HELPER:           FinishReason.output_budget_exhausted
                                EvaluationResult.output_budget_exhausted
                                output_budget_consistency_problems()
NEW_COUNT_METRIC:               operational.output_budget_exhaustion_count
NEW_RATE_METRIC:                operational.output_budget_exhaustion_rate
DENOMINATOR:                    generations that produced output and are classified
PER_FAMILY_BREAKDOWN:           YES     PAIRED_DIAGNOSTIC: YES (observational only)
LEGACY_ALIAS:                   operational.input_truncation_rate (same Metric object)

INPUT_TRUNCATION_SEMANTICS_CHANGED:  NO      TIMEOUT_SEMANTICS_CHANGED: NO
OG3_CHANGED:                         NO      GATE_LOGIC_CHANGED:        NO
D38_READ_BY_ANY_GATE:                NO      SECURITY_VETO_ADDED:       NO
GENERATION_BEHAVIOR_CHANGED:         NO      MAX_NEW_TOKENS: 512_UNCHANGED

OLD_METRIC_POLICY_HASH:  2d0830103bc11f280fc2a25e5ac8f0f79bd3e6a1ad589046d238e9fc5d9cfd87
NEW_METRIC_POLICY_HASH:  e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a
GATE_POLICY_HASH:        e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5 (unmoved)
GENERATION_POLICY_HASH:  c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7 (unmoved)

HISTORICAL_S3I_BASELINE / CANDIDATE:  0/36 (0.000000) · 1/36 (0.027778)
HISTORICAL_S3L_BASELINE / CANDIDATE:  0/36 (0.000000) · 5/36 (0.138889)
HISTORICAL_BASELINE_EOS:              72 / 72
CONSISTENCY_MISMATCHES:               0 of 144

CANDIDATE_001 / CANDIDATE_002:        EVALUATED_NOT_ELIGIBLE, both unchanged
CANDIDATE_003_CREATED / EVAL_V4:      NO / NO
EVAL_V4_REQUIRED_BEFORE_CANDIDATE_003: YES
TRAIN_TOKEN / EVAL_TOKEN:             NOT CREATED / NOT CREATED
MODEL_GENERATIONS / OPTIMIZER_STEPS:  0 / 0
MODEL_PROMOTION:                      NOT_AUTHORIZED   MODEL_REGISTRY_MUTATED: NO
MERGE / TAG / RELEASE / VERSION_BUMP: NO / NO / NO / NO
```

---

## 20 — Exact NEXT

**S3M.2 is closed and authorises nothing further.** D38 is fixed as an observability
property. It is **not** evidence that a third candidate will score better, it predicts
nothing, and it must not be presented as a target to optimise.

The instrument semantics are now frozen: **D37 fixed, D38 fixed**. The next step is a
**separate, operator-controlled milestone**, and its prerequisites are not details:

1. **Read D37 + D38 as frozen instrument authority.**
2. **Create and freeze a fresh `m62-defensive-eval v4` BEFORE any training.** `eval-v3` is
   used, and S3M.2's retrospective draws on its body-free results.
3. **Design candidate 003 as a controlled experiment changing EXACTLY ONE primary
   model/training axis** — future training rendering `MODEL_DEFAULT` → `DISABLED`.
4. **Keep LoRA module scope `ATTENTION_AND_MLP`.** Combining the rendering axis with
   `ATTENTION_ONLY` moves two variables and makes the result uninterpretable.
5. **Keep candidate 002's measured training configuration otherwise fixed**, unless
   repository evidence establishes a blocking incompatibility.
6. **Perform no live training until a separate `TRAIN` authorisation**, and no evaluation
   until a separate single-use `EVAL` authority at a new generation.

**The D38 metric is not a model axis.** It is observational, it applies symmetrically to
baseline and candidate, and candidate 003's eligibility remains determined only by the
already-declared security, quality, format and operational gates. **Do not predeclare that
candidate 003 must improve the D38 number**, and **do not modify the D38 metric after
seeing candidate-003 outputs.**

**Do not** raise `max_new_tokens`, add structured rows, strengthen the response schema,
change gates/graders/thresholds/the refusal detector, or fix **D39** as a rider.
