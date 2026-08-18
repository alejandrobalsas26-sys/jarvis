# V69 M62 S3Q.0 — qualifying the one-shot evaluation ceremony, before it runs

> **Status: `S3Q0_EVAL_CEREMONY_QUALIFICATION: PASS`.** This milestone prepares the
> airlock. It does **not** open the exam.
>
> **Zero model weights loaded, zero generations, zero response tokens, zero held-out
> reads, no `EVAL` authority created, no confirmation string materialised for any real
> plan, no plan consumed.** `m62-defensive-eval v4` remains **`FROZEN_UNUSED`**,
> `spent_by: null`. `qwen3-06b-lora-quality-live-003` remains **`TRAINED_UNEVALUATED`**.
>
> **Nothing here predicts candidate 003's result, and nothing here authorises measuring
> it.**

| | |
|---|---|
| Milestone | V69 M62 **S3Q.0** — evaluation ceremony qualification + Control Plane generation 4 |
| Date | 2026-08-17 |
| Branch | `jarvis-v69-m62-training-gym` |
| Starting HEAD | `05c043b3a89cdb675846abb8aabf1f476c6d7796` |
| Master | `3705114228edef2f665be349c5c4429b7b16777a` (untouched) |
| Host | Kali Linux mini-PC, CPU only, repository system interpreter |
| Preceding | **S3P** — `V69_M62_S3P_CANDIDATE003_LIVE_TRAINING.md` |

---

## 1 — Authorisation and boundary

The operator authorised **only** the qualification and hardening of the one-shot held-out
evaluation ceremony: exact-plan binding, prospective holdout-commit governance, ledger
durability, body-blind interfaces, portable evaluation-receipt infrastructure, synthetic
qualification, and the corresponding control-plane update.

Measured rather than asserted:

```
MODEL_WEIGHTS_LOADED:                   NO
CANDIDATE003_ADAPTER_LOADED_FOR_INFER:  NO
MODEL_GENERATIONS:                      0
MODEL_RESPONSE_TOKENS:                  0
LIVE_EVAL_ATTEMPTS:                     0
HUMAN_LIVE_EVAL_AUTHORIZATION:          NO
LIVE_CONFIRMATION_STRING_MATERIALIZED:  NO
LIVE_CONFIRMATION_STRING_EMITTED:       NO
LIVE_PLAN_CONSUMED:                     NO
EVAL_V4_SEMANTIC_BODY_ACCESS:           NO
EVAL_V4_PROGRAMMATIC_BODY_ACCESS:       NO
FULL_HISTORICAL_PROGRESS_READ:          NO
D28 / D33 / D38 / D39:                  OPEN / OPEN / FIXED_OBS_ONLY / OPEN, unchanged
GENERATION / METRIC / GATE POLICY:      UNCHANGED
MERGE / TAG / RELEASE / VERSION_BUMP:   NO / NO / NO / NO
```

**S3Q.0 is an infrastructure milestone.** It spent no capability and measured no model.

---

## 2 — Why this milestone exists

`eval-v4` can be read exactly once. Everything downstream of that read — the eligibility
verdict, the promotion decision, the scientific claim — inherits whatever integrity the
ceremony had at the moment the first held-out task reached the model. Independent review
raised six questions about that ceremony. All six were reproduced from CURRENT source
before anything was changed, and none was taken on trust.

---

## 3 — The six findings, independently reproduced

### FINDING A — plan consumption is not holdout spend

**CONFIRMED.** `training_gym/evaluation/store.py` had exactly two writers: `consume_plan`
(`event: "started"`) and `record_terminal` (`event: <terminal state>`). There was no third
event, and therefore no durable record of the moment a held-out corpus crossed the
model-facing boundary.

Those are different facts. A run can consume its plan and then fail while building the
pack, and no model has read anything. Conflating them either re-spends a holdout that was
read or writes off one that was not.

### FINDING B — the plan's pack and target digests were proxies

**CONFIRMED.** `scripts/evaluate_adapter.py::_plan()` bound:

```python
task_pack_hash = sha256({"dataset": manifest.manifest_hash(),
                         "counts": {split: n}})
hidden_target_store_hash = sha256(manifest.manifest_hash())
```

while execution derived `built.pack.pack_hash()` and `built.targets.store_hash()` from the
material itself. `pack_builder.py`'s own module docstring already recorded the gap — *"the
CLI therefore planned an evaluation from split counts read out of a manifest and bound a
placeholder digest where the pack hash belonged"* — and closed it only on the execution
side. Planning was never wired to it.

Consequence: one confirmation authorised a **family** of possible packs. A builder change,
a grader-registry change or a schema change produced a different pack from the same
manifest, under the same approved digest, with no complaint from anything.

### FINDING C — the order assignment was a proxy

**CONFIRMED.** The plan bound `sha256(f"{ORDER_POLICY_BALANCED}:{seed}")`. The runner's
canonical `order_assignment_hash(pack, seed)` digests `{policy, seed, assignment}` where
the assignment is derived per task from `task.task_hash`. An operator approved a policy
name and a seed; the run executed an ordering nobody had computed at approval time.

### FINDING D — an executable plan claimed it would not run a model

**CONFIRMED.** `EvaluationPlan.performs_inference` defaults `False`. `_plan()` never set
it. `_execute()` passes **that exact object** to `execute_evaluation`, which loads weights
and generates. So the effect list an operator read before approving — the one surface
whose whole purpose is to say what approval permits — was wrong about the only effect that
matters.

### FINDING E — a terminal-ledger failure returned clean success

**CONFIRMED.** `execution._record()` caught every exception, appended a sentence to
`outcome.problems`, and returned. `ExecutionOutcome.ok` was `state is COMPLETED` and
nothing else, so `_execute()` printed the artefacts and returned `EXIT_OK`. A one-shot
evaluation with no durable record of how it ended was reported to the operator as fine.

### FINDING F — a plan-ledger failure escaped, and left a blocking orphan

**CONFIRMED.** `consume_plan(...)` was called **outside every `try`** in
`execute_evaluation`, one line after `create_generation_directory`. An append failure:

* escaped as a bare exception (the CLI's catch-all reported `EXIT_INTERNAL`);
* left the plan **unspent** — correct;
* left the freshly created generation directory behind — **not** correct, because
  `check_output_root` and `create_generation_directory` then refused every later attempt
  at that generation.

A full disk was enough to deadlock a generation number permanently.

---

## 4 — The current execution order, independently derived

Read from source rather than from the review's list:

```
 1  config load                        scripts/evaluate_adapter.py::_load
 2  baseline reference verification    _references -> base_reference_from_identity
 3  adapter reference verification     _references -> build_adapter_reference
 4  reference-pair verification        verify_reference_pair
 5  dependency probe                   _dependency_report
 6  hardware probe                     HardwareCapabilityReport.detect
 7  dataset manifest read              load_manifest
 8  EXACT PACK IDENTITY PREPARATION    prepare_pack_identity            [S3Q.0, new]
 9  plan construction                  EvaluationPlan(...)
10  blocker check                      plan.blockers
11  confirmation check                 check_evaluation_confirmation
12  replay check                       is_plan_consumed
13  dependency readiness gate          dependencies.ready
14  baseline live-blocker gate         baseline.live_blockers
15  generation-directory creation      create_generation_directory
16  plan consumption                   consume_plan                     [now guarded]
17  task-pack materialisation          build_task_pack_from_dataset
18  hidden-target store materialised   (same call)
19  TOCTOU RE-VERIFICATION             execution_binding_mismatches     [S3Q.0, new]
20  pack blocker check                 pack_blockers
21  backend object creation            backend_factory x2
22  runner begins                      run_paired_evaluation
23  EvaluationRequest construction     both arms, per task
24  parity verification                parity_hash comparison
25  HOLDOUT MODEL-FACING COMMIT        record_holdout_commit            [S3Q.0, new]
26  first _invoke                      runner._invoke
27  first backend.generate             backend.generate
28  model/tokenizer load               transformers_peft (live only)
29  prompt encoding                    (live only)
30  first model.generate               (live only)
31  scoring                            build_comparison
32  gates                              evaluate_gates
33  artifact writing                   write_evaluation_artifacts
34  artifact verification              verify_evaluation_generation
35  state COMPLETED                    machine.to(COMPLETED)
36  terminal ledger append             record_terminal                  [now durability-critical]
```

Steps 8, 19 and 25 are new. Step 16's failure route and step 36's failure semantics
changed. Nothing between steps 28 and 33 was touched.

---

## 5 — Plan truthfulness audit

Every field whose name implies an exact runtime identity, classified before and after.

| Field | Before S3Q.0 | After S3Q.0 |
|---|---|---|
| `task_pack_hash` | **PROXY** — digest of manifest + split counts | **EXACT_RUNTIME_IDENTITY** — `EvaluationTaskPack.pack_hash()` |
| `hidden_target_store_hash` | **PROXY** — digest of the manifest digest | **EXACT_RUNTIME_IDENTITY** — `HiddenTargetStore.store_hash()` |
| `order_assignment_hash` | **PROXY** — `sha256("<policy>:<seed>")` | **EXACT_RUNTIME_IDENTITY** — `runner.order_assignment_hash(pack, seed)` |
| `expected_task_count` | DETERMINISTIC_INPUT_BINDING — manifest counts | **EXACT** — `len(pack)` |
| `expected_baseline_generations` | DETERMINISTIC_INPUT_BINDING | **EXACT** — equals task count |
| `expected_candidate_generations` | DETERMINISTIC_INPUT_BINDING | **EXACT** — equals task count |
| `expected_grader_executions` | **ESTIMATE** — `task_count * 6` | **ESTIMATE**, unchanged and labelled |
| `expected_files` | DETERMINISTIC_INPUT_BINDING (allowlist) | unchanged |
| `expected_state_transitions` | DETERMINISTIC_INPUT_BINDING (planning states) | unchanged |
| `backend_id` | EXACT | unchanged |
| `performs_inference` | **MISDESCRIBED** — `false` on an executable plan | **EXACT** — `true` |
| `generation_policy_hash` | EXACT_RUNTIME_IDENTITY | unchanged |
| `grader_policy_hash` | EXACT_RUNTIME_IDENTITY | unchanged |
| `metric_policy_hash` | EXACT_RUNTIME_IDENTITY | unchanged |
| `statistical_policy_hash` | EXACT_RUNTIME_IDENTITY | unchanged |
| `gate_policy_hash` | EXACT_RUNTIME_IDENTITY | unchanged |
| `family_policy_hash` | EXACT_RUNTIME_IDENTITY | unchanged |
| `resource_policy_hash` | EXACT_RUNTIME_IDENTITY | unchanged |

`expected_grader_executions` stays an **ESTIMATE** and is not promoted to an exact
identity: the real count depends on per-family grader registries and on how many arms
produce scoreable output, neither of which is knowable before the run. Naming it exactly
would be the same defect in a new place.

### The structural enforcement

`EvaluationPlan.__post_init__` now refuses a plan with **no blockers** whose
`task_pack_hash`, `hidden_target_store_hash` or `order_assignment_hash` is not a 64-hex
digest. An executable plan that cannot name the material it authorises is not
constructible. A plan that genuinely cannot build the pack carries a blocker saying so and
is refused by `is_executable` — **MISSING is not FALSE**.

---

## 6 — The exact-binding architecture

`training_gym/evaluation/preflight.py` is the **single materialisation authority**.

```
             prepare_pack_identity(root, dataset, splits, generation, seed)
                                    |
                       build_task_pack_from_dataset      <- body-opaque
                                    |
                        derive_pack_identity(built, seed)
                                    |
                              PackIdentity               <- body-free
                             /              \
                     planning                execution
              (binds into the plan)   (compares against the plan)
```

There is **one** implementation of each digest, and it belongs to the object that owns it:
`pack.pack_hash()`, `targets.store_hash()`, `runner.order_assignment_hash()`. Neither the
planner nor the executor computes a digest of its own — `hashlib.sha256` no longer appears
in either file, and a test asserts that.

**The pack is not cached across the confirmation boundary.** Holding a built pack — and
therefore a live `HiddenTargetStore` — in process state across a human decision point
would put held-out answers in mutable memory across that decision, and would let a plan
approved against one on-disk state execute against a pack materialised before it. So the
pack is built, hashed and dropped, and execution rebuilds it deterministically.

**TOCTOU.** Immediately before the boundary, `execution_binding_mismatches` re-derives and
compares: pack hash, hidden-target-store hash, order policy, order assignment hash, dataset
manifest hash, expected task count, baseline reference hash, candidate adapter reference
hash, tokenizer identity hash, generation policy hash. Any mismatch raises **before** the
holdout is committed. The plan is already spent at that point; the holdout is not.

---

## 7 — Three read boundaries, kept apart

| Boundary | Meaning | S3Q.0 status |
|---|---|---|
| **A. `ORCHESTRATOR_SEMANTIC_ACCESS`** | A human or AI sees prompt / target / response text | **FORBIDDEN** for `eval-v4`. Not performed. |
| **B. `BODY_OPAQUE_PROGRAMMATIC_ACCESS`** | Reviewed code reads body bytes to validate, hash or canonicalise, and returns digests and counts only | **PERMITTED**, exercised only against the S3Q.0 **synthetic** corpus |
| **C. `MODEL_FACING_ACCESS`** | A held-out task crosses the backend boundary | The prospective scientific spend. **Not performed.** |

Conflating B with A is how a firewall gets argued away, so the distinction is structural
rather than procedural: `PackIdentity` has no field that could hold material, and
`file_evidence()` returns exactly `{sha256, bytes, record_count}` from a file that may be
full of held-out prompts. `record_count` is deliberately not called `records` — a field
named for the thing it counts is one refactor away from holding it.

### The eval-v4 firewall in this milestone

`corpus_v4_material()` was never called. No v4 shard was materialised, no v4 prompt,
target or hidden target was opened, no v4 blob was read for meaning, the v4 pack builder
was not run against v4, and no v4 canary exists. Every body-firewall test in this milestone
runs against `s3q0-synthetic-eval v1`, whose manifest digest `def03f88…` is distinct from
every real M62 dataset and whose material is marked with canaries that appear nowhere in
the real corpora.

---

## 8 — The prospective holdout spend ruling

> **A fresh holdout becomes scientifically `USED_IMMUTABLE` when the evaluator DURABLY
> commits the first held-out `EvaluationRequest` to the model-facing backend invocation
> boundary.**

This is **governance + observability hardening**, not observability alone. It is a real
tightening of when a corpus counts as spent, and it is stated as one.

**It applies PROSPECTIVELY.** Candidate 001 (S3I, eval-v2) and candidate 002 (S3L,
eval-v3) were measured before the event existed. Their ledgers are sealed, carry only
`started` and a terminal line, and are **not** reinterpreted, rewritten or retrofitted.

**Why this boundary and not "after `model.generate` returned".** There is no atomic
transaction between a durable local append and an external synchronous call. One of the
two gaps must be taken on faith, and the fail-closed direction is to assume the model read
the material. The cost is that a crash between the append and the call marks a holdout
spent that a model may not have read. That is the conservative error, and it is the one
this repository chooses deliberately.

### The event

| | |
|---|---|
| Name | `holdout_model_facing_committed` |
| Record version | `m62.evaluation_holdout_commit.1` |
| Body version | `m62.evaluation_holdout_commit_body.1` |
| Home | the canonical evaluation ledger, `evaluation_runs.jsonl` |

**Not** `model_read`: the record cannot prove a forward pass already happened. **Not**
`holdout_exposed`: nothing was exposed to a human or an orchestrator. It means exactly:
*the first held-out request has passed parity checks and the evaluator has durably
committed to hand it to the production backend.*

**Position** — after both arms' requests exist, after parity is proved, after the
execution order is known, and immediately before the first `_invoke`:

```
requests constructed -> parity verified -> [DURABLE COMMIT] -> _invoke -> backend.generate
```

The seam is `run_paired_evaluation(before_first_model_facing_invoke=...)`, a one-shot
callback owned by `execute_evaluation`. It is execution infrastructure and is kept out of
the measurement: it enters no `parity_hash`, no model input and no policy digest. A test
proves the run hash is byte-identical with and without it.

**Body** (closed field list, refused if widened or incomplete): commit schema version,
dataset id / version / manifest hash, task pack hash, hidden-target store hash, pack
identity hash, order policy, order assignment hash, task count, target count, first task
id, first task hash, first arm, first request parity hash, baseline reference hash,
candidate adapter reference hash, generation policy hash, backend id, performs_inference.

No prompt, no system prompt, no target, no rubric, no expected answer, no response, no
confirmation literal, no private path, no username, no hostname.

**Uniqueness.** At most one commit per `(evaluation_id, generation, plan_hash)`. A second
commit for the same plan, or a commit for the same generation under a different plan, is
refused **before** the append. A commit for a plan with no `started` line is refused too —
ordering enforced by the ledger rather than trusted of the caller.

**Fail-closed.** If the append fails, the exception propagates and `backend.generate` is
never called. Expected state: `PLAN_CONSUMED: YES`, `HOLDOUT_MODEL_FACING_COMMITTED: NO`,
`MODEL_FACING_CALLS: 0`. No automatic retry, no new token, operator recovery required.

**Once it succeeds**, the holdout is `USED_IMMUTABLE` regardless of what follows: backend
readiness failure, dependency failure, model or tokenizer load failure, adapter load
failure, OOM, a `model.generate` exception, interruption, artifact failure, scoring
failure, or terminal-ledger failure. No second attempt against the same holdout.

---

## 9 — Legacy ledger compatibility

`LEGACY_EVALUATION_LEDGER_COMPATIBILITY: PASS`.

The tracked reader of the evaluation ledger is `is_plan_consumed`, which filters on
`event == "started"` and is unaffected by a new event name. The commit line declares its
own `record_version` rather than widening `m62.evaluation_run.1`, so every historical line
stays byte-valid, unedited and readable. The four historical runs in the local ledger
(`qwen3-06b-lora-live-eval-001` gen-2 and gen-3, `m62-s3i-quality-heldout-live` gen-1,
`m62-s3l-quality-heldout-live` gen-1) carry two lines each and continue to.

No retrospective marker was synthesised for any of them. A receipt cannot be built for a
run that has no commit line, and that refusal is tested — the correct outcome for a
historical run is "no modern receipt exists", never a fabricated one.

---

## 10 — The states that are not the same state

| State | Plan | Holdout | Meaning |
|---|---|---|---|
| Blocked plan | unspent | unread | Nothing happened. |
| Directory creation failed | unspent | unread | Nothing happened. |
| **Plan-ledger append failed** | **unspent** | unread | The approval survives. The empty directory this attempt created is withdrawn. No automatic retry. |
| **Plan consumed, no commit** | **spent** | **unread** | Real, and neither "nothing happened" nor "the holdout is spent". The token is gone; the exam is untouched; operator recovery is mandatory. |
| **Committed** | spent | **spent** | `USED_IMMUTABLE` whatever happens next. Rerun forbidden. |
| Committed, terminal append failed | spent | spent | The measurement is valid, its durable outcome record is missing. Recovery, never rerun. |

`ExecutionOutcome` records `plan_consumed`, `holdout_committed`, `terminal_recorded` and
`durability_problems` as four separate facts, because each fails separately.

---

## 11 — Durability hardening

### `ok` now means what it says

```python
ok = (state is COMPLETED
      and holdout_committed
      and terminal_recorded
      and not durability_problems)
```

A durability-critical problem is one that loses a guarantee the ceremony is judged on. It
is kept apart from `problems` and from gate blockers, so a diagnostic warning cannot be
mistaken for a lost guarantee and vice versa. The synthetic corpus legitimately carries a
gate blocker (16 pairs, below the policy minimum of 30) and still reports `ok`.

### The terminal-ledger rule

If the artefacts are valid and the terminal append fails: the holdout stays
`USED_IMMUTABLE`, the plan stays spent, the artefacts are **retained and not
overwritten**, the evaluation **must not be re-run**, and the CLI returns the new
`EXIT_DURABILITY` (22) with an explicit recovery message. Nothing is discarded and nothing
is repaired automatically.

### The plan-ledger rule

The append is now guarded. On failure the outcome is structured (no traceback, no host
path), the plan is unspent, no backend is called, and the generation directory this
invocation created is **withdrawn with `rmdir` only** — and only when it is provably still
empty. A directory with anything in it is left exactly where it is and reported, because
it might be somebody's evidence. `shutil.rmtree`, `unlink` and `os.remove` do not appear
in the module, and a test asserts that.

### Recovery versus rerun

`RECOVERY` and `RERUN` are different operations. After a model-facing commit, **RERUN is
FORBIDDEN**. Recovery may only verify existing evidence, append or repair missing body-free
control-plane evidence under a future explicit recovery authority, and seal the
already-consumed holdout state. S3Q.0 documents those requirements and **creates and
exercises no recovery authority**.

---

## 12 — Token-silent live preflight

`--live-preflight` derives the exact plan and reports body-free metadata **without**
materialising `EVAL:<plan-hash>`.

**Emits:** `plan_hash`, `is_executable`, blockers, warnings, live-execution blockers,
dependency readiness, backend id, both reference hashes, tokenizer identity, config hash,
dataset id / version / manifest hash, exact `task_pack_hash`, exact
`hidden_target_store_hash`, order policy, exact `order_assignment_hash`, task count, split
/ family / kind counts, pack identity hash, all seven policy hashes, dependency and
hardware report hashes, `performs_inference`, the expected effect list, the authority
*form* `EVAL:<plan-hash>`, `configured_timeout_s` and `timeout_enforced: false`.

**Never emits:** `confirmation_required`, `confirmation_token`, any `EVAL:<64-hex>`
literal, any task, target or response body. Both properties are asserted in code
(`assert_body_free`, `assert_token_silent`), not merely intended.

### This is hygiene, not cryptography

The confirmation string is a pure function of `plan_hash`, which the preflight prints.
Nothing here makes the token secret, unpredictable or authenticating, and token silence
prevents no theft. Its purpose is to avoid accidental materialisation, avoid accidental
shell or document persistence, separate PRE-GO from GO, and make the timing of human
authorisation explicit.

### Token-emitting surfaces, documented not fixed

| Surface | Emits the full literal? |
|---|---|
| `EvaluationPlan.confirmation_token()` | **YES** — by definition |
| `EvaluationPlan.to_record()` | **YES** — `confirmation_required` |
| `--print-plan` | **YES** — prints `to_record()` |
| `--dry-run` | **YES** — `confirmation_required` |
| `--live-preflight` | **NO** |
| `EvaluationReport` / `--verify-report` | NO |
| `ModelCandidateProposal` | NO |

A future PRE-GO ceremony must not use a token-emitting surface.

---

## 13 — Artefact body classification

Measured with canaries, not declared.

| Artefact | Classification |
|---|---|
| `evaluation-plan.json` | `BODY_FREE` |
| **`task-pack.jsonl`** | **`BODY_BEARING`** — carries `system_prompt` and `user_prompt`; it is what the model is handed |
| `task-pack-manifest.json` | `BODY_FREE` |
| `baseline-results.jsonl` | `BODY_FREE` — `response_sha256` + `response_chars`, never `response_text` |
| `candidate-results.jsonl` | `BODY_FREE` |
| `paired-comparisons.jsonl` | `BODY_FREE` |
| `baseline-scores.jsonl` | `BODY_FREE` |
| `candidate-scores.jsonl` | `BODY_FREE` |
| `metrics.json` | `BODY_FREE` |
| `evaluation-report.json` | `BODY_FREE` |
| `evaluation-manifest.json` | `BODY_FREE` |
| ledger records (all three events) | `BODY_FREE` |
| portable evaluation receipt | `BODY_FREE` |

`task-pack.jsonl` is `BODY_OPAQUE_VERIFICATION_ONLY` for every automated consumer:
`ORCHESTRATOR_SEMANTIC_READ: FORBIDDEN`, programmatic hashing permitted. It still carries
no target and no response — the answer key never enters the pack.

**Exception safety.** Pack-validation failures, hidden-target lookup failures, artifact
verification failures and binding-mismatch refusals were each induced and their messages
scanned: none echoes a prompt, target or response canary.

---

## 14 — The portable evaluation receipt

Built **before** the irreversible act, deliberately. S3P had to invent portable training
evidence after the training run; a one-shot evaluation gets no such second chance.

`scripts/build_m62_eval_receipt.py` distils one completed generation into a tracked,
root-independent document. Schema `m62.eval_receipt.1`, enforced by
`eval_receipt_schema()` and published byte-identically at
`state/m62/schema/m62-eval-receipt.schema.json` (strict, `additionalProperties: false`,
every security-relevant field enum- or pattern-bound).

**It binds:** candidate identity and status claim, adapter reference, baseline reference
and tokenizer identity, holdout identity (dataset id / version / manifest / pack /
hidden-target-store / shard digests / counts), exact plan identity and its schema version,
order policy and assignment, `performs_inference`, `binds_exact_pack_identity`, all eight
policy and environment digests, the spent authority (form only, one creation, one
consumption, no retry), the three durable ledger events with their counts, the
model-facing commit's own identity, execution counts and artifact verification, every
evidence digest including per-file digests of the body-bearing pack, and the outcome with
its gate blockers and limitations.

**It never carries** a prompt, target, rubric or response; a confirmation literal; an
absolute path, home directory or username; or a verdict of its own.

**Determinism.** Serialised with the control plane's one `canonical_json`, carrying no
timestamp. `receipt_hash` is the digest of the payload with that field removed —
self-checking without being self-referential. Rebuilding from the same evidence reproduces
the same bytes; changing any one bound fact fails verification (proved over eight
independent mutations).

**It refuses to describe an incomplete ceremony:** no start line, two start lines, no
commit line, two commit lines, no terminal line, or a commit naming a different pack are
each a refusal rather than a receipt.

### A receipt is evidence, never authority

`grants_no_further_authority: true`, `retry_authorized: false`,
`token_literal_recorded: false`, `promotes_model / activates_model /
mutates_model_registry: false`. No receipt authorises TRAIN, EVAL, a retry, promotion,
activation, registry mutation, a merge or a release.

---

## 15 — Anti-circularity in the control plane

`check_evaluation_receipt` is `check_training_receipt`'s argument one door further in, and
the door is the irreversible one.

**For candidate 003 and every future modern-receipt candidate**, a transition into any
`EVALUATED_*` state requires a tracked, schema-valid, digest-checked portable evaluation
receipt that describes **that** candidate, whose eligibility **supports** the state
claimed, that binds exactly one of each durable event, whose plan bound the exact pack
identity and declared `performs_inference`, and whose holdout agrees with the control
plane's own dataset record.

Insufficient, individually and together: the snapshot alone; the snapshot plus a hardcoded
expectation in the verifier; PROGRESS prose; a human sentence saying it passed.

Proved in **both** directions over the whole `EVALUATED_*` vocabulary — a snapshot claiming
`EVALUATED_NOT_ELIGIBLE` and one claiming `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` are equally
refused without a receipt. **Nothing here predicts which candidate 003 will obtain.**

**Legacy.** `LEGACY_EVALUATION_CANDIDATES` names candidates 001 and 002 and is **closed**.
They were measured before this evidence form existed; their sealed milestone documents are
their authority; they are not retrofitted, because synthesising a receipt for a run that
never emitted one is manufacturing evidence, which is strictly worse than declaring the
gap. Candidate 003 is deliberately **not** in the set.

The `evaluation_receipt` snapshot field is **optional by shape and mandatory by
semantics**: absent from the schema's `required` list so superseded snapshots stay
structurally valid without being rewritten, and refused by the semantic check whenever a
modern candidate claims an evaluated state without one. Absence is not permission.

---

## 16 — Version movements, each explained

| Identity | Before | After | Why |
|---|---|---|---|
| `EVALUATION_PLAN_SCHEMA_VERSION` | `m62.evaluation_plan.1` | **`m62.evaluation_plan.2`** | The field list is unchanged; three fields' MEANING changed from proxy to exact. A confirmation issued under version 1 approved a manifest digest and must not be honoured for material it never named. |
| `EVALUATOR_VERSION` | `m62.s3c.1` | **`m62.s3q0.1`** | A verdict is attributable to code. The code that produces one now binds the exact pack, durably commits the holdout, and refuses to report a completed run whose evidence is missing. |
| ledger record vocabulary | `m62.evaluation_run.1` | unchanged, **plus** `m62.evaluation_holdout_commit.1` | Extension, not migration. Legacy lines stay valid and unedited. |
| `EVALUATION_PREFLIGHT_VERSION` | — | `m62.evaluation_preflight.1` | New. Versions the BINDING, not any model behaviour. |
| `m62.eval_receipt.1` | — | new | New evidence form. |
| **`generation_policy_hash`** | `c6b0b682…` | **UNCHANGED** | No measurement changed. |
| **`metric_policy_hash`** | `e07dd133…` | **UNCHANGED** | No measurement changed. |
| **`gate_policy_hash`** | `e50033194…` | **UNCHANGED** | No measurement changed. |

**Plan hashes will move**, and that is correct: exact binding and truthful inference are
supposed to change the digest. There is no authorised live candidate-003 plan to preserve,
and a hash preserved across a truthfulness fix would mean a token issued against the
misleading description still authorised the run.

---

## 17 — Defects: unchanged

**D28 — OPEN, unchanged.** No tool-call transport exists; `tool_call_validity_rate` is
VACUOUS on both arms and the six `tool_call_schema` tasks decide nothing. Not fixed as a
rider. **The future evaluation report must restate it.**

**D33 — `OPEN_UNCHANGED`.** Verified from current source: `timeout_s` participates in
`GenerationPolicy.to_dict()` and therefore in `policy_hash()`; the production
`transformers_peft` backend contains no `signal`, no thread, no subprocess, no watchdog and
no external timeout wrapper. So:

```
TIMEOUT_S_FUTURE_CONFIG:      300
TIMEOUT_CONFIGURATION_BOUND:  YES
TIMEOUT_ENFORCED:             NO
TIMEOUT_RATE:                 VACUOUS under the current instrument
EXTERNAL_TIMEOUT_ADDED:       NO
```

No rider fix was made, and every S3Q.0 surface that reports the configured ceiling reports
`timeout_enforced: false` beside it.

**D38 — `FIXED_OBSERVABILITY_ONLY`, unchanged.** No gate reads output-budget exhaustion,
none was added, `max_new_tokens` is still 512.

**D39 — `OPEN_UNCHANGED`.** Not fixed as a rider.

---

## 18 — Qualification evidence

### Synthetic full-path result

The **real** orchestration path — same `execute_evaluation`, planner primitive, runner,
graders, comparison, gates, report, artifact writers, ledger, state machine and receipt
builder — driven over `s3q0-synthetic-eval v1` (16 tasks, 3 splits, 4 families, all three
task kinds) with deterministic doubles, a temporary runtime root, no model, no `eval-v4`
and no candidate 003.

`classify_empirical_status` marks every such run `SYNTHETIC_ONLY`, which `decide_eligibility`
refuses before it looks at a single gate — so no synthetic ceremony here can ever conclude
that an adapter is eligible.

### Required order trace, instrumented

```
generation_directory_created
  < plan_consumed
  < task_pack_ready
  < exact identity re-verified against the confirmed plan
  < evaluation_request_constructed
  < parity_verified
  < holdout_model_facing_commit_durable
  < first_backend_generate
  < scoring
  < artifact_validation
  < terminal_ledger_durable
  < receipt_valid
```

### Failure injection matrix

| | Scenario | Plan | Holdout | Model calls | Result |
|---|---|---|---|---|---|
| F1 | plan blockers | unspent | uncommitted | 0 | PASS |
| F2 | generation-directory creation fails | unspent | uncommitted | 0 | PASS |
| F3 | plan-ledger append fails | unspent | uncommitted | 0 | PASS — structured, orphan withdrawn |
| F4 | pack rebuild fails after consumption | spent | uncommitted | 0 | PASS — no auto retry |
| F5 | pack hash differs from the confirmed plan | spent | uncommitted | 0 | PASS |
| F6 | hidden-target-store hash differs | spent | uncommitted | 0 | PASS |
| F7 | order-assignment hash differs | spent | uncommitted | 0 | PASS |
| F8 | holdout-commit append fails | spent | uncommitted | **0** | PASS — hard failure |
| F9 | commit succeeds, then `generate` raises | spent | **committed** | ≥1 | PASS — spent, one failed arm, no retry |
| F10 | commit succeeds, then backend blocked | spent | committed | — | PASS — spent, no retry |
| F11 | interruption after commit | spent | committed | — | PASS — no retry |
| F12 | artifact validation fails | spent | committed | — | PASS — quarantined, no retry |
| F13 | terminal append fails after valid artefacts | spent | committed | — | PASS — **not** clean success, artefacts retained, recovery |
| F14 | complete synthetic evaluation | spent | committed | all | PASS — exactly one of each event, receipt valid |
| F15 | duplicate holdout commit | — | — | — | PASS — refused |
| F16 | terminal completion without a commit | — | — | — | PASS — no receipt |
| F17 | receipt without terminal evidence | — | — | — | PASS — refused |
| F18 | receipt without plan-start evidence | — | — | — | PASS — refused |

### Non-vacuity

* **Exact plan.** Pack hash, target-store hash, order assignment, candidate reference,
  baseline reference, tokenizer identity, generation policy hash and dataset manifest each
  mutated **independently**; each refusal names the field it caught; every one refuses
  before the model-facing commit. The three superseded proxy formulas are recomputed and
  asserted **different** from what is now bound.
* **Body firewall.** Three canaries injected into prompts, targets and responses. Absent
  from the preflight, the pack identity, every ledger line, the receipt, the outcome, the
  CLI-facing payload, every body-free artefact and every induced exception message.
  Present, as expected and as classified, in `task-pack.jsonl`.
* **Terminal durability.** The pre-S3Q.0 behaviour is induced by injection and asserted
  gone: `ok` is false, `recovery_required` is true, the artefacts survive, rerun is
  forbidden.
* **Per-task retries.** `CANONICAL_PER_TASK_RETRIES: 0`, verified rather than assumed —
  one call per task-arm, no duplicates, and a backend that raises produces one recorded
  failure and no second call.

### New test suites

| Suite | Tests |
|---|---|
| `test_training_gym_m62_s3q0_plan_truthfulness.py` | 30 |
| `test_training_gym_m62_s3q0_exact_binding.py` | 22 |
| `test_training_gym_m62_s3q0_holdout_commit.py` | 25 |
| `test_training_gym_m62_s3q0_ledger_durability.py` | 15 |
| `test_training_gym_m62_s3q0_body_blindness.py` | 31 |
| `test_training_gym_m62_s3q0_eval_receipt.py` | 32 |
| `test_training_gym_m62_s3q0_control_plane.py` | 27 |
| Shared synthetic scaffolding | `tests/_s3q0_synthetic.py` |

---

## 19 — One pre-existing test was rescoped, and why it is not a regression

`test_s3n_changed_no_evaluation_policy_grader_or_gate_source` diffed the S3N starting
commit against the **working tree**, so it silently asserted that no *later* milestone had
touched `training_gym` either. That held through S3N.1, S3O and S3P by coincidence — none
of them changed production evaluation source — and stopped holding when S3Q.0 hardened the
plan binding and the ledger under an explicit authority to do so.

Reading that as an S3N regression would be wrong twice: S3N is sealed and cannot regress,
and a later milestone's authorised change is not evidence about an earlier one's
discipline. The second endpoint is now pinned to S3N's closing commit `ec446e3`, so the
test measures exactly the property its name states — and that property still holds: S3N's
own range touched no file under `jarvis/training_gym/`.

S3Q.0's own scope is guarded by its own suite: `gates.py`, `metrics.py`, `scoring.py`,
`statistics.py`, `comparison.py`, `generation.py`, `policy.py`, `task_pack.py`,
`pack_builder.py`, `score_evidence.py`, both backends, every grader and the whole
`training_gym/training/` tree are asserted untouched since `05c043b`.

---

## 20 — Final control-plane state

```
candidate 001         EVALUATED_NOT_ELIGIBLE      (legacy, no receipt, not retrofitted)
candidate 002         EVALUATED_NOT_ELIGIBLE      (legacy, no receipt, not retrofitted)
candidate 003         TRAINED_UNEVALUATED         evaluation_corpus null, no receipt
eval-v4               FROZEN_UNUSED               spent_by null
train authority       NONE_OBSERVED_IN_REPOSITORY
eval authority        NONE_OBSERVED_IN_REPOSITORY
promotion authority   NONE_OBSERVED_IN_REPOSITORY
control plane grants  FALSE
```

The authority observation keeps its existing semantics: it is measured as "no tracked file
carries a token literal" and is **not** proof that none exists elsewhere.

---

## 21 — EXACT NEXT

> **A NEW CLAUDE CODE SESSION: the one-shot evaluation of candidate 003 against a
> simultaneously measured baseline on frozen `eval-v4` — ONLY after a NEW EXPLICIT HUMAN
> `EVAL` AUTHORIZATION.**
>
> **S3Q.0 does not grant that authorization.**

If it is later granted, the sequence is: derive the exact plan on the executing host in
`.venv-m62-eval-linux` → run `--live-preflight` and review it → a fresh single-use `EVAL`
token → one paired run against `eval-v4` → build and verify the portable evaluation
receipt → a new control-plane generation. D28, D29 and D33 must be restated in that
report.

**Still ruled out:** re-running a spent evaluation · a second `TRAIN` capability for
candidate 003 · reading `eval-v4` bodies to explain, debug or tune anything · turning a v4
failure into a training example · ranking 001, 002 and 003 in one table · a second
experimental axis · any LoRA, LR, epoch or seed change · raising `max_new_tokens` ·
changing gates, graders, thresholds or the refusal detector · creating a D38 gate · fixing
D28, D33 or D39 as a rider · promotion, activation, registry mutation, merge, tag, release
or version bump.
