# V69 · M62 · S3X.0 — pre-spend holdout firewall breach: recovery and body-blindness hardening

> **Scope.** This milestone measures nothing. No model was loaded, no token was
> generated, no `EVAL` authority was created or consumed, and no held-out corpus was
> spent. Candidate 004 remains `TRAINED_UNEVALUATED`. `eval-v5` remains `FROZEN_UNUSED`
> with `spent_by` null.
>
> **Body-free.** This document contains no prompt, no target, no hidden target, no
> reference answer, no tool-cache content and no operator path. It records digests,
> statuses, counts and mechanisms only.

---

## 0 — What happened, in one paragraph

A session began S3W.1 — candidate 004's live held-out evaluation against `eval-v5`.
Before any human `EVAL` authorization existed, and before any of the durable
model-facing boundaries could be crossed, **one `eval-v5` prompt body was rendered into
the orchestration session** by nothing more than Python's default representation
machinery. The session aborted immediately, at the right boundary, and created no
measurement.

Two facts have to be held at once, and this milestone exists because collapsing them
would be a lie in either direction:

| | |
|---|---|
| **No evaluation occurred.** | Proved below from repository authority, not memory. |
| **A held-out body did reach an orchestrator.** | Permanently true. A fixed bug does not retroactively un-see it. |

The first fact means nothing needs repairing. The second means the freshness property
`eval-v5` was frozen to carry is gone, and no amount of infrastructure work brings it
back.

---

## 1 — Starting authority, verified before anything was written

| Fact | Value |
|---|---|
| Branch | `jarvis-v69-m62-training-gym` |
| HEAD at start | `7dafb66f8d0e4611b1d77aedcfa3ed3614bfab02` |
| Origin | identical, divergence `0 0` |
| `master` | `3705114228edef2f665be349c5c4429b7b16777a`, untouched |
| Worktree | clean |
| Control plane schema | `m62.control_plane.1` |
| State generation | 11 |
| Snapshot | `state/m62/snapshots/0011-m62-s3w0-candidate004-eval-ready.json` |
| Snapshot SHA-256 | `3c85eadff59a00c37d08161330cbb0c3c630ddfef527308549febcc0ed502603` |
| Subject commit | `4d75d3faa66ccb5388b8e6ced621cc0878f680e3` |

`python jarvis/scripts/verify_m62_control_plane.py` — **PASS**, `PROBLEMS: 0`, before any
edit. Nothing was repaired.

---

## 2 — The scientific non-event, proved rather than asserted

Every line below is a repository observation. None is recalled from a conversation.

### 2.1 No receipt

`state/m62/receipts/` holds exactly three files. The only candidate 004 entry is
`qwen3-06b-lora-quality-live-004.train.json`. **There is no
`qwen3-06b-lora-quality-live-004.eval.json`.**

### 2.2 No run

`jarvis/evaluation/evaluations/` contains four run directories — `qwen3-06b-lora-live-eval-001`,
`m62-s3i-quality-heldout-live`, `m62-s3l-quality-heldout-live`,
`m62-s3q-quality-heldout-live`. **No `m62-s3w1-*` directory exists.**

### 2.3 No ledger entry

The append-only evaluation ledger holds 11 entries. Its last activity is
**2026-08-18**, the S3Q ceremony against candidate 003. Body-free summary:

| Evaluation id | Events | Holdout commit |
|---|---|---|
| `qwen3-06b-lora-live-eval-001` | started, completed (×2 generations) | — |
| `m62-s3i-quality-heldout-live` | started, completed | — |
| `m62-s3l-quality-heldout-live` | started, completed | — |
| `m62-s3q-quality-heldout-live` | started, **`holdout_model_facing_committed`**, completed | v4 |

**No `started` event for candidate 004 exists.** The only
`holdout_model_facing_committed` event in the entire ledger belongs to S3Q and `eval-v4`.
The ledger is body-free by construction — every entry passes
`assert_no_private_content` before it is appended — so reading it is not a body access.

### 2.4 No authority

The aborted session derived a plan hash but was never authorized to act on it:

```
ABORTED_PLAN_HASH   137a828a86c6d12238f40b14ddba6049860c85e157af21f49d30157ef4668a5c
AUTHORIZED          NO
EVAL_AUTHORITY_CREATED    0
EVAL_AUTHORITY_CONSUMED   0
HOLDOUT_SPENT       NO
```

This hash is recorded here as **incident evidence, never as authority**. `git grep` over
the whole repository finds it in no tracked file, and the token `EVAL:<that hash>` has
never been materialised anywhere tracked. It is written down so the incident can be
identified later, and for no other purpose.

### 2.5 The summary line

| | |
|---|---|
| `HUMAN_EVAL_AUTHORIZATION` | NO |
| `EVAL_AUTHORITY_CREATED` | 0 |
| `EVAL_AUTHORITY_CONSUMED` | 0 |
| `MODEL_WEIGHT_LOADS` | 0 |
| `MODEL_GENERATIONS` | 0 |
| `EVAL_ATTEMPTS` | 0 |
| `HOLDOUT_SPEND_EVENTS` | 0 |
| `MODEL_FACING_HOLDOUT_ACCESS` | **NO** |
| `ORCHESTRATOR_HOLDOUT_BODY_ACCESS` | **YES** |

---

## 3 — The exposure, stated exactly

**Class:** `PRE_AUTH` · `PRE_MODEL_LOAD` · `PRE_GENERATION` · `PRE_HOLDOUT_SPEND`.

**Scope, as reported by the aborted session:** one `eval-v5` prompt body was incidentally
rendered by `repr`. No target, no hidden target, no reference answer.

That scope is taken from the incident report as **provenance for the fact that exposure
occurred**. It is not treated as permission to inspect held-out material, and this
session did not verify it by looking. The prompt was not reopened, not quoted, not
hashed to re-identify it, and not counted. This session's own standing is therefore:

```
ORCHESTRATOR_EVAL_V5_SEMANTIC_ACCESS   NO
```

Confirming the scope by reading the prompt would have been a second exposure performed in
order to describe the first.

### Untracked residue

The aborted session left untracked, git-ignored residue on the host: tool-result cache
files containing the incidental output, a reconstructed runtime corpus, temporary
verification roots, and an S3W.1 evaluation config.

```
UNTRACKED_PREVIOUS_SESSION_RESIDUE_REPORTED   YES
TRACKED_HOLDOUT_BODY_LEAK                     NO
```

None of it was opened, read, grepped for content, or copied into the repository. No
operator home-directory path is recorded here. **Deleting that residue would not undo the
exposure**, and no such claim is made anywhere in this milestone; cleanup is host hygiene
and is not scientific recovery.

One consequence is visible and is recorded rather than smoothed over: the untracked
S3W.1 config causes
`test_training_gym_m62_s3w0_eval_qualification.py::test_no_candidate_004_evaluation_evidence_exists_anywhere`
to fail, because that test scans `jarvis/evaluation/` for the candidate identifier and
the v5 digests. The failure **pre-exists this milestone** — it reproduces on a pristine
checkout of `7dafb66` with no changes applied — and it is the firewall test doing its
job, not a regression. It was not made green by weakening the test.

---

## 4 — Root cause: three ordinary features that compose into a leak

Reproduced with **synthetic canaries only**
(`SYNTHETIC_PROMPT_CANARY_DO_NOT_PERSIST`, `SYNTHETIC_TARGET_CANARY_DO_NOT_PERSIST`).
`eval-v5` was not involved in the reproduction.

1. **`@dataclass` generates a `__repr__` that renders every field.** `EvaluationTask`
   holds `system_prompt` and `user_prompt`, so its default repr printed both. Nine
   body-bearing dataclasses across `training_gym` were in this state, none of them
   carrying `repr=False` on a single body field.

2. **A container's repr recurses into its elements.** `EvaluationTaskPack.tasks` is a
   `tuple[EvaluationTask, ...]`, so one repr of the pack rendered the whole corpus.

3. **`repr` of a bound method interpolates `repr(method.__self__)`.** Python renders a
   bound method as `<bound method C.m of {self!r}>`. Displaying `pack.pack_hash`
   **without calling it** therefore rendered every task body in the pack.

Route 3 is the one that fired, and it is the reason a fix aimed only at `repr(pack)`
would have been insufficient: there is no `__repr__` on a method object to override. The
only available defence is that the pack's **own** repr — which the method repr
interpolates — must already be body-free.

Measured on the pre-fix tree, **13 of 13 probes leaked** the synthetic canary:

`repr(task)` · `repr(pack)` · `repr(pack.counts_by_split)` · `repr(pack.counts_by_family)` ·
`repr(pack.pack_hash)` · `repr(pack.to_dict)` · `repr(task.identity_hash)` · `str(pack)` ·
f-string · `%r` · `repr(hidden_target)` · `repr(ht.to_dict)` · exception interpolation.

### Why the existing firewall did not catch it

`test_training_gym_m62_s3q0_body_blindness.py` is a mature suite, and it was not
defective. It proves that written **artefacts** and refusal **messages** carry no body.
The incident touched neither: it was an in-memory **display**. The gap was a category the
firewall did not have — representation — not a hole in the categories it had.

---

## 5 — The fix

`training_gym/schemas.py` gains `body_free_repr(obj, *fields, **extra)`. Every
body-bearing dataclass installs a `__repr__` built from it, naming identity, digest and
status fields only.

The guard **does not trust the caller's field list**. Every value still passes through a
renderer that allows only atoms, enums, and strings that are short and single-line;
anything else is reduced to type and size (`<str len=412>`, `<tuple len=36>`,
`<dict keys=1>`). A future contributor who adds `"user_prompt"` to a safe-field list by
mistake still cannot leak, because a prompt is not identifier-shaped.

One override closes every route at once — `repr`, `str`, f-strings, `format`, `%s`, `%r`,
logging, exception interpolation, tracebacks **and every bound method** — because all of
them ultimately call it.

| Module | Class | Bodies it held |
|---|---|---|
| `evaluation/task_pack.py` | `EvaluationTask` | `system_prompt`, `user_prompt` |
| `evaluation/task_pack.py` | `HiddenTarget` | `target_text` |
| `evaluation/task_pack.py` | `EvaluationTaskPack` | the task tuple |
| `evaluation/backend.py` | `EvaluationResult` | `response_text` |
| `datasets/candidate.py` | `DatasetCandidate` | `system_message`, `user_prompt`, `target_text` |
| `datasets/preference.py` | `RejectedResponse` | `text` |
| `task_spec.py` | `TaskSpec` | `prompt` |
| `teachers/base.py` | `ProviderResponse` | `text` |
| `teachers/cloud.py` | `CloudResponse` | `text` |

### One class deferred, and why

A ninth body-bearing dataclass — `ConvertedRecord` in
`training_gym/training/dataset_conversion.py` — **keeps the generated repr**.
`jarvis/training_gym/training/` is under a permanent freeze asserted against the working
tree by
`test_training_gym_m62_s3q0_control_plane.py::test_the_graders_and_the_refusal_detector_are_untouched`.
S3X.0 is not entitled to edit it.

The guard was written, the freeze test caught it, and the change was **reverted** rather
than argued around. The deferral is defensible on its merits as well as its authority:
`ConvertedRecord` holds TRAINING corpus rows and never evaluation-holdout material, so it
is outside this incident's blast radius.

It is a deferral with a named owner, not a silent allowlist.
`test_the_frozen_training_surface_is_the_only_reason_for_the_exclusion` asserts the freeze
still exists, and **fails the moment that freeze is lifted** — forcing the decision to be
retaken on its merits rather than inherited.

Debuggability was deliberately preserved — safety that destroys it gets removed by the
next person in a hurry. The bound-method repr still identifies its object:

```
<bound method EvaluationTaskPack.pack_hash of EvaluationTaskPack(
    dataset_id='...', dataset_version='...', generation=1, task_count=36)>
```

### What the fix does not touch

Dataset bodies · dataset hashes · evaluation semantics · generation policy · scoring ·
gates · task ordering · model behaviour. It is representational only.

---

## 6 — Proof that no measurement semantics moved

A deterministic 36-task synthetic pack was built and **20 families of identity** were
serialised to canonical JSON — pack hash, `to_dict`, `to_record`, task ordering, task
hashes, task identity hashes, per-task dicts, prompt hashes, system-prompt hashes, target
hashes, hidden-target dicts, counts by split, family and kind, splits, families,
eligibility blockers, generation policy hash, generation policy dict, and the task-record
digest.

The probe was run twice: once inside a detached worktree of the pristine starting HEAD
`7dafb66`, once on the hardened tree.

```
BEFORE   98fe45c58779241f88a3936a099ece237c2202890a97c050e92cc8894ce03ab1
AFTER    98fe45c58779241f88a3936a099ece237c2202890a97c050e92cc8894ce03ab1
```

**Byte-identical across all 71 722 bytes.** `SEMANTIC_EVALUATION_IDENTITIES_CHANGED: NO`.

This was structurally expected and is now also measured: every identity in this
repository is derived through `canonical_json`, and `repr` appears in no hashing or
serialisation path.

---

## 7 — The tests, and why they are not vacuous

`jarvis/tests/test_training_gym_m62_s3x0_repr_body_blindness.py` — 32 tests.

- **Direct representation** — `repr`, `str`, f-string, `format`, `%s`, `%r` over task,
  pack and hidden target.
- **The bound-method route** — every callable bound to the object is enumerated with
  `dir()` and swept, rather than the one method that happened to be typed. A companion
  test asserts the sweep actually finds `pack_hash`, `to_dict`, `counts_by_split` and
  `counts_by_family`, so it cannot pass by sweeping nothing.
- **Logging, exceptions and tracebacks** — `%r` interpolation through a real
  `logging.Handler`, exception construction, and a formatted traceback.
- **The architectural invariant** — `test_no_body_bearing_dataclass_relies_on_the_generated_repr`
  walks every module in `training_gym`, finds every dataclass holding a body-shaped field,
  and requires each to define `__repr__` in its own module. The dataclass-generated repr
  is compiled inside `reprlib`, so comparing the code object's filename against the class's
  filename separates the two **with no allowlist to rot**.

Non-vacuity is established three ways:

1. A test asserts the canaries are genuinely present in the synthetic bodies.
2. Two mutation tests restore a body-rendering `__repr__` and require the same probes to
   go **red** — including, explicitly, the bound-method route.
3. An end-to-end mutation was run against the real source: deleting
   `EvaluationTask.__repr__` turned **12 tests red**, including the architectural
   invariant. Restoring it returned the suite to green.

```
BODY_BLINDNESS_TEST_NONVACUOUS   YES
```

The suite also asserts, over its own source, that it names no real corpus, no v5 digest,
and performs no file read.

---

## 8 — Defect D44

| Field | Value |
|---|---|
| `id` | **D44** |
| `status` | `FIXED` |
| `is_gate` | **true** |
| `evidence` | this document |

> Body-bearing evaluation pack objects could expose held-out task bodies through default
> Python `repr`, including `repr` of a bound method whose `__self__` is the body-bearing
> pack. A body-free representation guard closes every route; measurement identities are
> byte-identical.

`is_gate` is `true` because a failure of body-blindness blocks a clean live-evaluation
ceremony: an orchestrator that can see the exam cannot preside over it. D44 is the first
defect in this ledger to carry `is_gate: true`. D25 and D28–D43 are untouched.

**D44 does not settle the scientific question.** A fixed repr bug does not make the
S3W.1 session retroactively body-blind. That is a separate ruling, and it is not a
decision this document is entitled to make.

---

## 9 — What `eval-v5` is, and what it is not

The distinction below must be preserved on every future surface, because collapsing it
in either direction records something untrue.

| Axis | Value | Why |
|---|---|---|
| **Physical / model-facing lifecycle** | `FROZEN_UNUSED`, `spent_by` null | The durable boundary was never crossed. Marking it `USED_IMMUTABLE` would be a lie. |
| **Scientific eligibility** | subject to a separate human ruling | A held-out prompt crossed the orchestration boundary before authorization. |

The `EVALUATION_HOLDOUT` schema admits exactly two statuses — `FROZEN_UNUSED` and
`USED_IMMUTABLE`. No third status was invented. Scientific disqualification is not a
dataset lifecycle state and is not represented as one; falsifying the lifecycle to
express a scientific judgement would corrupt the one field that records what a model was
actually shown.

**None of the following is true and none may ever be written:** v5 was evaluated · v5 was
model-spent · candidate 004 saw v5 · candidate 004 failed v5.

---

## 10 — Candidate 004 is unmeasured

```
status               TRAINED_UNEVALUATED
evaluation_corpus    null
evaluation_receipt   null
```

The aborted session created no measurement, so candidate 004 is not failed, not
ineligible, not eligible, not evaluated and not quarantined. Its eligibility is
**UNKNOWN**, exactly as it was at generation 11.

---

## 11 — Requirements for a future `eval-v6` (body-free, preregistered)

**S3X.0 creates no `eval-v6` bodies.** This session holds the incident context and
therefore must not become the author of the replacement holdout. It may only specify
structure.

A future `eval-v6` freeze must:

- occur in a **new, dedicated holdout-builder session**;
- occur **after** candidate 004's weights and configuration are already immutable — they are;
- perform **zero** candidate 004 model loads and **zero** generations;
- use **no** candidate 004 output and **no** `eval-v5` semantic content;
- **not** read the exposed v5 prompt;
- **not** tune task design from candidate 004 behaviour;
- preserve the established evaluation shape unless an independent, predeclared reason exists.

### Expected structural target

Re-derived body-free from current S3S policy authority and confirmed against the S3W.0
shape assertions:

| | |
|---|---|
| Tasks | 36 |
| Splits | 12 hidden · 12 security · 12 adversarial |
| Families | 12 safety_refusal · 9 structured_report · 9 evidence_request · 6 tool_call_schema |
| Decision kinds | 12 refusal · 6 required_completion · 18 completion |

### Freshness standard, stated precisely

`eval-v5` was **candidate-blind in the strong temporal sense**: frozen by S3S before
candidate 004 existed. That property is not recoverable, because candidate 004 exists
now. Claiming it for v6 would be false.

The achievable and sufficient standard is:

```
POST-TRAIN_FROZEN         v6 is frozen after candidate 004's weights are immutable
CANDIDATE_OUTPUT_BLIND    no candidate 004 output informed any v6 task
UNMEASURED_CANDIDATE      candidate 004 has never been measured against v6 or any successor
```

These are **evidence properties, not dataset statuses**. No dataset status bearing these
labels exists or may be invented.

### Session separation

| Milestone | Role |
|---|---|
| **S3X.0** | incident recovery, hardening, guard — this document |
| **S3X.1** | fresh `eval-v6` authoring and freeze, in a new session |
| **S3Y** | `eval-v6` qualification and live ceremony, in a session that did **not** author v6 |

The session that authors v6 bodies may not be the session that evaluates against them,
for the same reason S3X.0 may not author v6: whoever has seen the exam cannot invigilate.

---

## 12 — Test result

Canonical invocation, from `jarvis/`, as recorded in the generation 11 baseline:
`pytest -k m62 --ignore=tests/test_live_brain_v61.py`

| | Generation 11 baseline | S3X.0 |
|---|---|---|
| passed | 4284 | **4312** |
| skipped | 20 | **20** |
| failed | 0 | **4** |

4284 + 32 new tests = 4316 = 4312 passed + 4 failed. **The hardening caused no
regression**; all four failures have two causes, neither of them the guard.

### Three are the two-phase transient, by design

```
s3n1_control_plane.py::test_no_state_bearing_production_path_changed_since_the_subject_commit
s3n1_control_plane.py::test_the_verifier_passes_on_the_repository_as_it_stands
s3u_control_plane.py::test_the_live_control_plane_verifies_clean
```

`verify_m62_control_plane.py` reports `STALE_STATE: FAIL` — 8 state-bearing production
paths changed since subject commit `4d75d3f` without a new state generation. **That is
the mechanism working.** The Phase A commit deliberately lands before the state
generation that describes it, exactly as this milestone's two-phase pattern requires, and
these three clear the moment generation 12 is sealed with the hardening commit as its
subject.

They are therefore **blocked on §13, not on the hardening**. Until the capacity blocker
is resolved, the control plane stays `FAIL` on this one check.

### One is the untracked residue

```
s3w0_eval_qualification.py::test_no_candidate_004_evaluation_evidence_exists_anywhere
```

Caused by the untracked S3W.1 residue config described in §3. It **reproduces on a
pristine checkout of `7dafb66` with no changes applied**, so it pre-exists this milestone
and is not a regression. It is the firewall test doing its job, and it was not made green
by weakening the test. Removing that untracked file is host hygiene, and is not this
milestone's to perform.

---

## 13 — Capacity: the recovery generation 12 does not fit

Generation 11 closed at **31 741 / 32 768** bytes — 1 027 of headroom. S3W.0 projected a
generation 12 at 31 109 bytes, but that projection assumed the S3W.1 **measurement**,
which replaces forward-looking qualification entries with results. A **recovery**
generation 12 replaces nothing: candidate 004 is still unmeasured, so every forward
entry stays live, and the incident, the ruling, the defect and the invariants are all
added on top.

Measured, not estimated:

```
GEN11_SNAPSHOT_BYTES              31741   HEADROOM  1027
PROJECTED_GEN12_FULL_BYTES        33833   HEADROOM -1065   over the hard cap
PROJECTED_GEN12_LEANEST_BYTES     32739   HEADROOM    29   under the cap, over the policy
REQUIRED_HEADROOM_BYTES            1024
GEN12_CAPACITY                     FAIL   short by 995 bytes
PROGRESS_BYTES                    40317 / 40960   HEADROOM 643   PASS
PROGRESS_LINES                      610 / 760     HEADROOM 150   PASS
```

Neither budget was raised. A clause-preserving recompaction of `limitations` was
attempted using the qualified S3W.0 strategy — replace superseded claims, merge entries
stating one fact at two granularities, compress filler — over six entries including two
genuine merges. It recovered **−133 bytes**: it made the block *larger*, because
preserving every clause of two merged entries costs more than the separator it saves.
S3W.0 had already taken that surface to its information-density limit and recorded that
two further merges were abandoned for the same reason.

There is no supported mechanism for archiving `limitations` or `defects`; the archive
covers the historical `PROGRESS.md` only. Defects D25 and D28–D43 may not be rewritten.

```
S3X0_STATE_CAPACITY_BLOCKER
```

**No fact was erased to make room.** The blocker is reported rather than resolved,
because the two ways to clear it — raising a reviewed budget, or dropping recorded
constraints — are both operator decisions, not a milestone's to take.

---

## 14 — What this milestone deliberately did not do

Evaluate candidate 004 · load model weights · generate a token · create or consume `EVAL`
authority · read an `eval-v5` body · verify the reported exposure scope by inspection ·
open, read or grep the untracked residue · copy residue into the repository · create
`eval-v6` bodies · mark `eval-v5` `USED_IMMUTABLE` · mark candidate 004 evaluated ·
invent a dataset status · rewrite D25 or D28–D43 · rewrite history · promote, merge, tag,
release or bump a version.
