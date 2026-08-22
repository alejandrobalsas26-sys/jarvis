# V69 M62 S3U — Candidate 004: one dial, and a human decision behind it

**Status** COMPLETE · design only · no scientific activity
**Subject** `jarvis/scripts/build_quality_training_config.py`
**Candidate** `qwen3-06b-lora-quality-live-004` — `DESIGNED_UNTRAINED`
**Primary axis** `learning_rate` · 1e-4 → 5e-5 · **one** dial
**Control plane** generation 8 → generation 9
**Scope** DESIGN. No training, no evaluation, no model load, no promotion.

This document is **body-free**. It contains no held-out prompt, no held-out target, no
response text, and no excerpt of any of them. `eval-v5` appears here only as a manifest,
a pack digest and a status.

| | |
|---|---|
| Model loads | **0** |
| Model generations | **0** |
| Optimizer steps | **0** |
| Train attempts | **0** |
| Evaluation attempts | **0** |
| `eval-v5` semantic access | **NO** |
| Adapters produced | **0** |
| TRAIN authority | **NONE** |
| EVAL authority | **NONE** |
| Promotion authority | **NONE** |

---

## 1 — The two things that had to happen before this milestone could exist

Generation 8 recorded two open human decisions, and S3S.1 §11 had already shown they
could not be resolved by analysis:

1. **Does M62 continue at all?**
2. **If it continues, which standing `ruled_out` entry is superseded, and at what
   generation?**

The second question was not "is the rank hypothesis reopened". S3S.1 §11 established the
broader and more uncomfortable finding: taken literally, the generation-7 `ruled_out`
list forbade **every** candidate-004 training axis that had been proposed.

| axis | barred by |
|---|---|
| A — LoRA capacity r16 → r8 | "any learning-rate, epoch, rank, alpha or dropout change" |
| B — `ATTENTION_ONLY` | "adding a second experimental axis, or ATTENTION_ONLY" |
| C — learning rate | "any learning-rate, epoch, rank, alpha or dropout change" |
| D — curriculum intervention | "adding structured rows…"; "creating train-v3, adding rows or rebalancing train-v2" |
| — raise the output ceiling | "raising max_new_tokens" |

**There was no permitted single training axis for candidate 004 under standing
authority.** Any candidate 004 at all required an operator decision superseding a
standing entry, whichever axis was chosen. S3S.1 issued none and recorded no preference
between superseding an entry and declining to build a fourth candidate.

This milestone is the session in which both decisions arrived.

---

## 2 — The operator ruling

**This is recorded as an OPERATOR DECISION, not as a model recommendation.** The two are
kept apart deliberately and are not collapsed anywhere in this repository:

> **MODEL / ANALYSIS RECOMMENDATION.** S3S.1 §10 ranked *lower learning rate / update
> magnitude* first among candidate-004 axes, promoting it above S3R's rank hypothesis.
> That is analysis. It authorised nothing, and S3S.1 says so in its own §14.

> **HUMAN OPERATOR RULING (S3U).** An explicit, single-purpose human authorisation
> permitted M62 to continue and permitted candidate 004 to be **designed** as a
> learning-rate single-axis experiment at **5e-5**.

| field | value |
|---|---|
| `M62_CONTINUES` | **YES** |
| `CANDIDATE004_PRIMARY_AXIS` | `learning_rate` |
| `REFERENCE` | `qwen3-06b-lora-quality-live-003` |
| `REFERENCE_LEARNING_RATE` | **1e-4** — re-derived, never quoted (§4) |
| `PROPOSED_CANDIDATE004_LEARNING_RATE` | **5e-5** |
| what it authorised | **design only** |
| what it did not authorise | training, evaluation, promotion |

The ruling phrase itself is deliberately **not persisted** in any tracked file. The
machine-readable record at `state/m62/rulings/0001-s3u-candidate004-learning-rate.json`
carries a SHA-256 digest of it and `ruling_phrase_recorded: false`, for the same reason
the training receipt carries `token_literal_recorded: false`: a control plane that stored
a replayable authorisation string would be holding a capability it is forbidden to hold.
A digest proves which phrase was given; it cannot be spent.

### 2.1 The exact narrow supersession

The standing entry, quoted verbatim from generation 8's `next_milestone.ruled_out`:

> "any learning-rate, epoch, rank, alpha or dropout change"

The ruling supersedes **one clause of that one entry**, prospectively, for **one
candidate**:

| clause | after the ruling |
|---|---|
| learning-rate change | **SUPERSEDED for candidate 004 only**, and only to 5e-5 |
| epoch change | still ruled out |
| rank change | still ruled out |
| alpha change | still ruled out |
| dropout change | still ruled out |

Nothing else in the list moves. `ATTENTION_ONLY`, module-surface changes, a second axis,
training-corpus modification, `train-v3`, structured-row or response-schema intervention,
`max_new_tokens`, gate/grader/threshold/refusal-detector changes, `eval-v5` mutation,
`eval-v4` reuse and retraining candidate 003 all remain ruled out and are all
**unchanged** by this design.

**The historical ruling is not erased.** It remains factual at the generation where it
was made, and generations 1-8 are immutable. Generation 9 records the newer human ruling
that supersedes one portion of it prospectively.

**The supersession is candidate-004-specific.** It is not a general permission to vary
learning rates, and the control-plane verifier now refuses a `ruled_out` rewrite that
reads as an unscoped learning-rate permission (`_check_ruled_out`).

---

## 3 — Why this axis, from the sealed evidence

Re-derived from the sealed development record, not from the ruling and not from the
session that produced this document.

**Candidate 003 showed general stopping pressure.** Against its own simultaneously
measured baseline on `eval-v4` it produced **6** output ceilings where the baseline
produced **2** — and it nevertheless improved several measured outcomes: task success
24/36 → 25/36, reward 0.5461 → 0.5903, and three security findings fixed. It was
`NOT_ELIGIBLE` on one deterministic gate, schema validity 9/9 → 8/9, which is one task.

**S3S.1 §7.2 established body-free that the adapter did not invent the failure family.**
Both observed ceiling phenotypes already occur in the base model at rank 0:

| | degenerate ceiling | normal-density ceiling |
|---|---:|---:|
| baseline | 1 | 1 |
| candidate 003 | 1 | 5 |

The adapter **cured** the base model's two ceilings and produced six of its own. It moved
*which inputs* trigger a pre-existing mechanism. That is the observation this axis rests
on: if the adapter redistributes rather than creates the failure, the natural single knob
is **how far** the fitted adapter moves the base model, not **how much capacity** it has.

**S3S.1 §10 therefore ranked lower update magnitude above the rank-capacity hypothesis**,
and recorded why the rank hypothesis weakened: a phenotype present at rank 0 makes
capacity a less likely controlling variable.

**S3T.0 then improved prospective observability and changed nothing else** — no scoring,
no gate, no eligibility rule, no generation behaviour (§9).

### 3.1 The hypothesis

> **Does reducing the update magnitude preserve candidate 003's useful behavioural
> improvements while reducing the harmful redistribution of the base model's stopping
> failure mode?**

This is a **HYPOTHESIS**. Nothing here claims causality is established.
`TRAINING_ROOT_CAUSE_CONFIDENCE` remains **NOT_ESTABLISHED** and this milestone does not
move it: three candidates all showed the phenotype, the base model shows it too, the
response bodies that could have adjudicated were never persisted, and the learning rate
has never been varied so no dose-response is measured.

The risk in the other direction is real and is recorded rather than discounted: S3S.1
rated the risk to candidate 003's safety gains from a weaker update as **HIGH**. A
candidate 004 that loses the three security improvements would be an informative result
about the same hypothesis, not a surprise.

### 3.2 Why 5e-5 and not some other reduction

Halving is the smallest reduction that yields a reading at all, and it places the
lineage on one line with two points already measured:

| candidate | learning rate | measured |
|---|---|---|
| 001 | 2e-4 | yes, on `eval-v2` |
| 002 | 1e-4 | yes, on `eval-v3` |
| 003 | 1e-4 | yes, on `eval-v4` |
| **004** | **5e-5** | **not yet** |

Those measurements are **not** a head-to-head series — each candidate was measured on a
different holdout with zero shared task instances, and each stays comparable only against
its own simultaneously-measured baseline. The value of the sequence is that the axis has
a direction and a precedent, not that the four numbers can be plotted against each other.

---

## 4 — Candidate 004: identity and reference

| field | value | how it is established |
|---|---|---|
| candidate id | `qwen3-06b-lora-quality-live-004` | next ordinal in the quality lineage; the convention is read off candidates 001-003, not asserted |
| ordinal | 4 | absent from generation 8 in the snapshot and from the generator; no collision |
| experiment name | `m62-s3u-defensive-quality-004` | unique; a shared name would let two runs write into one history |
| state after S3U | `DESIGNED_UNTRAINED` | the canonical pre-training state; no new vocabulary invented |
| reference candidate | `qwen3-06b-lora-quality-live-003` | declared in `CANDIDATE_SINGLE_AXIS` |
| option | `S3U` | used by candidate 004 and by nothing else |
| reference option | `S3J` | candidate 003 shares candidate 002's option **by key** |

**The reference learning rate is re-derived, never quoted.** `CANDIDATE_OPTION["003"]`
resolves to `S3J`, and `OPTIONS["S3J"]["learning_rate"]` is **1e-4**. That is the dial
candidate 003 was configured under, read from the tracked generator that configured it.

Candidate 004's option is **derived by dictionary expansion** from `S3J`:

```python
OPTIONS["S3U"] = {
    **OPTIONS[S3U_REFERENCE_OPTION],
    ...,
    CANDIDATE_004_PRIMARY_AXIS: CANDIDATE_004_LEARNING_RATE,
    ...,
}
```

This is the single-axis guarantee, not a comment claiming one. Rank, alpha, dropout,
weight decay, warmup ratio, epochs, optimizer steps and gradient accumulation are **not
re-typed**, so there is no second place for them to drift to and no edit that can move one
by accident. A copied option is a thing that can drift; a derived one cannot.

---

## 5 — The single-axis matrix

`SINGLE_AXIS_DIFF_COUNT: 1` · `SINGLE_AXIS: learning_rate`

Measured by building both configurations against the same roots and diffing the canonical
bodies. `run_id`, `experiment_name` and `notes` are consequences of the candidate's
identity, not of its training behaviour, and are named explicitly — an unlisted difference
is a second axis.

```
RAW DIFF KEYS:   ['experiment_name', 'learning_rate', 'notes', 'run_id']
SEMANTIC DIFF:   ['learning_rate']
```

| field | candidate 003 | candidate 004 | moved |
|---|---|---|:--:|
| base model id | `Qwen/Qwen3-0.6B` | same | no |
| base model revision | `c1899de289a04d12100db370d81485cdf75e47ca` | same | no |
| tokenizer id / revision | as above | same | no |
| chat template digest | `a55ee1b1…1974d8` | same | no |
| training corpus | `m62-defensive-quality-train v2` | same | no |
| corpus manifest | `24ceb1e0…18fc0f` | same | no |
| training export identity | `82780fa0…7ae921` | same | no |
| validation shard | `ae6ffe20…1bb764` | same | no |
| validation export identity | `ac065112…d540fd` | same | no |
| dataset reference hash | `b3e1be3e…5f54b4` | same | no |
| reasoning policy | `DISABLED` | same | no |
| LoRA module surface | `ATTENTION_AND_MLP` | same | no |
| LoRA target modules | the seven Qwen3 projections | same | no |
| LoRA rank | 16 | 16 | no |
| LoRA alpha | 32 | 32 | no |
| alpha / r | 2.0 | 2.0 | no |
| LoRA dropout | 0.05 | 0.05 | no |
| bias policy | `none` | same | no |
| task type | `CAUSAL_LM` | same | no |
| epochs | 2 | 2 | no |
| max steps | 40 | 40 | no |
| warmup ratio | 0.1 | 0.1 | no |
| weight decay | 0.0 | 0.0 | no |
| optimizer / scheduler | backend default `adamw_torch`, linear decay | same | no |
| gradient accumulation | 8 | 8 | no |
| batch size | 1 | 1 | no |
| effective batch | 8 | 8 | no |
| sequence length | 512 | 512 | no |
| seed | 42 | 42 | no |
| precision | `fp32` | same | no |
| device | `cpu` | same | no |
| gradient checkpointing | false | false | no |
| packing | not a config surface | same | no |
| validation strategy | `epoch`, diagnostic only | same | no |
| checkpoint strategy | `no` | same | no |
| early stopping / load-best | hard-coded off in the backend | same | no |
| **learning rate** | **1e-4** | **5e-5** | **YES** |

If that diff count is ever anything but 1, the design is refused rather than built.

### 5.1 No slaved variable

Unlike the rank hypothesis, which would have required alpha moved with it to hold
`alpha/r` constant, a learning-rate change requires **no** compensating adjustment.
`alpha/r` is unchanged at 2.0 because *neither term moved* — not because a second
adjustment happened to land on the same ratio. A "compensating" hyperparameter change
here would be a second axis wearing a justification, and is refused as one.

### 5.2 How the claim is enforced, not just asserted

`verify_single_axis(candidate)` runs inside `build_config`, so a second dial cannot reach
a configuration document, a plan, a plan hash or a run. It refuses three distinct ways
this experiment could be worthless:

| condition | refusal |
|---|---|
| any dial other than the axis differs from the reference | *"a second experimental axis"* |
| a dial is deleted rather than changed | same — a missing key is a difference |
| candidate 004's rate equals candidate 003's | *"tests nothing"* — a re-run under a new name |
| candidate 004's rate is anything but 5e-5 | *"the operator ruling for it is 5e-5"* |
| an option that merely *agrees* with a shared-by-key reference | *"dials that agree today are not dials that are the same"* |

The control-plane verifier then re-derives the same relation independently: it asks the
production generator to refuse its own design, recomputes the dial diff, and requires it
to equal the declared axis. A snapshot claiming `DESIGNED_UNTRAINED` while the generator
cannot produce that design is a **failure**, not a pass.

---

## 6 — Configuration identities

`config_hash` and `plan_hash` bind `output_root_id`, runtime and hardware evidence.
They are therefore facts about one filesystem and one interpreter, and this document
deliberately **pins neither**. Re-derive them on the executing host; never paste a
recorded value in. The control plane pins the root-independent surfaces instead — corpus,
base revision, render policy, module surface and the single-axis relation — which
identify the design everywhere.

The dry-run plan for candidate 004 is **not executable** on the authoritative interpreter
and reports its blockers honestly: the training profile is not installed there and the
model weights are not known to be cached under a deny download policy. That is the
correct state for a design milestone. No plan token is derived here, none is printed, and
none is stored.

---

## 7 — Training corpus: `m62-defensive-quality-train v2`, unchanged

`TRAINING_CORPUS_CHANGED: NO`

Re-derived from disk by the same loader that verified it for candidate 003, and checked
against candidate 003's **tracked** portable training receipt rather than against a second
computation in the same process:

| identity | value |
|---|---|
| dataset | `m62-defensive-quality-train` |
| version | `v2` |
| manifest | `24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f` |
| export manifest | `82780fa0edc4c99198d0074a8a01b08507fa3eed54b4af50c3e045d5e07ae921` |
| train shard | `a02797f85d11498103918df9114ed4496e232a9a2c88b738f36f8326a72e1c7e` |
| validation shard | `ae6ffe204df4d2b60b2215aa38a641331cf56d999cc022c24f538fba891bb764` |
| validation export | `ac065112c4cb3a2195100c3f11289d1e109f40441d293ded280d9b6cddd540fd` |
| dataset reference hash | `b3e1be3ed7e41953f874493a398c2dc3bd2267321d32d45572a5b4ba95f54a5c` |
| records | 182 — 154 TRAIN, 12 VALIDATION as converted |

No new rows. No row deletion. No rebalancing. No `train-v3`. No target edits, no
refusal-row edits, no structured-row edits, no response-schema strengthening. The corpus
is `USED_IMMUTABLE` and stays so.

Train-time validation remains 12 rows of **train-side steering material**. It appears in
no gate, it is not held-out evidence, and it is not comparable with any other candidate's
numbers.

---

## 8 — D37 stays fixed and is inherited, not re-decided

`D37: FIXED_UNCHANGED`

Candidate 004 inherits candidate 003's reasoning-policy correction **by assignment from
the reference**, not by a second literal that spells the same thing:

```python
CANDIDATE_REASONING["004"] = CANDIDATE_REASONING[CANDIDATE_004_REFERENCE_KEY]
```

| | |
|---|---|
| training reasoning policy | `DISABLED` |
| future evaluation reasoning policy | `DISABLED` |
| relation | the **same object**, `TRAIN_EVAL_PARITY_REASONING_POLICY is ELIGIBILITY_REASONING_POLICY` |

D37 is not reopened and is not an axis. Re-typing the value here would make train/eval
parity a coincidence two edits could separate, which is the exact thing D37's fix removed.

---

## 9 — D43 is prospective observability, and it is not an axis

`D43: FIXED_OBSERVABILITY_ONLY` · `D43_IS_GATE: NO`

Candidate 004's future evaluation will naturally run under the currently-qualified
instrumentation S3T.0 introduced. That is **environmental**: it is the current state of
the evaluation infrastructure, not a second experimental variable.

| property | value |
|---|---|
| body-free JSON parse diagnostics | present in the current instrumentation |
| `response_unique_char_ngram_ratio` | present |
| `RAW_RESPONSE_PERSISTED` | **NO** |
| `OBSERVABILITY_IS_GATE` | **NO** |
| `SCORING_CHANGED_BY_CANDIDATE004` | **NO** |

D38 and D43 diagnostics are **not gates** and must not be converted into hidden success
criteria. Reaching the output ceiling is not, by itself, failure.

---

## 10 — `eval-v5` binding: body-free identity only

`EVAL_V5_SEMANTIC_ACCESS: NO`

`eval-v5` was frozen **candidate-blind by S3S, before candidate 004 existed in any form**.
This milestone maintains that boundary. No task body, no target, no hidden target, no
source-corpus body and no prompt was read, and none appears here.

| field | value |
|---|---|
| dataset | `m62-defensive-eval` |
| version | `v5` |
| manifest | `e852f4627d4fe631f58ee3d120d5d1a81c94480a1c0b84e590d2b08261043f4c` |
| pack | `287a9fb61e3feab510763d834f77a75c3a016fe27ba4d04a4ac86c588c09fed6` |
| parent manifest | `8c6871b0094bdfc75062a6352d383fa8e9750c1425182a2b3248db20500081c5` (eval-v4) |
| task count | 36 |
| status | **`FROZEN_UNUSED`** |
| `spent_by` | **null** |

There is **no evaluation plan**, **no EVAL authority**, and no evaluation design in this
milestone. `eval-v4` is `USED_IMMUTABLE` and may never decide eligibility again.

### 10.1 Leakage between `train-v2` and `eval-v5`

Candidate 004 trains on the same `train-v2` corpus already checked during S3S, so that
qualification is **re-bound** rather than reconstructed — reconstructing it in a
candidate-design session would require semantic access this milestone must not take.

| check | result |
|---|---|
| `EXACT` | **CLEAN** |
| `LEXICAL` | **CLEAN** |
| `SEMANTIC` | **NOT_QUALIFIED** |

`SEMANTIC` has never run anywhere in this repository, and no model or embedding was
loaded here to change that. A pure paraphrase would not be caught. This is a standing
limitation, restated rather than newly discovered.

---

## 11 — Authority: none of it is created here

| authority | state |
|---|---|
| `TRAIN_AUTHORITY` | **NONE** |
| `EVAL_AUTHORITY` | **NONE** |
| `PROMOTION_AUTHORITY` | **NONE** |

S3U creates no live TRAIN authority, encodes no reusable token, and pre-consumes nothing.
The next live training milestone requires a **new, explicit, single-use human
authorisation in a fresh session**. Evaluation requires, separately and later, a
successful training run followed by a token-silent preflight and a fresh human EVAL
authorisation at a new generation. No promotion authority exists in this repository at
all, and no document creates one — `PROSE_CANNOT_GRANT_AUTHORITY` is a frozen invariant
and this document is prose.

---

## 12 — Success criteria: pre-registered, and deliberately qualitative

**No new `eval-v5` threshold is invented here.** Future eligibility will be decided by the
frozen evaluation machinery and the frozen canonical gate policy, unchanged.

Pre-registered qualitative hypothesis:

> Candidate 004 should retain candidate 003's useful safety and quality improvements
> while reducing the harmful redistribution of the base model's stopping failure mode.

Two things this explicitly does **not** say:

- Candidate 004 does **not** need zero output ceilings to qualify. Only the frozen gates
  decide eligibility, and no gate reads a ceiling count.
- D38 and D43 diagnostics are **not** gates and are **not** hidden success criteria. Any
  attempt to treat them as such is a gate change, which is ruled out.

### 12.1 What would falsify the hypothesis

Recorded before the measurement exists, so it cannot be chosen afterwards:

| observation | reading |
|---|---|
| ceilings unchanged or worse at 5e-5, with the reference's gains retained | the update magnitude does not control the redistribution — **falsifies** the axis as the controlling knob |
| ceilings reduced but the three security improvements lost | update magnitude controls both together; the axis is real but the trade is unfavourable — the S3S.1 **HIGH** risk realised |
| ceilings reduced and improvements retained | **consistent with** the hypothesis; still one observation on one holdout, and not causal proof |
| nothing measurably moves | the dose between 1e-4 and 5e-5 is too small to read; the axis is not falsified, but this candidate does not test it |

None of these outcomes is a promotion decision, and none of them is eligibility.
Eligibility is whatever the frozen gates say.

---

## 13 — Limitations

- **Causality is not established and is not claimed.**
  `TRAINING_ROOT_CAUSE_CONFIDENCE: NOT_ESTABLISHED`. The learning rate has never been
  varied in this lineage, so there is no measured dose-response and this candidate would
  produce a single new point rather than a curve.
- **The evidence class that could have adjudicated does not exist.** S3S.1 established
  that the spent `eval-v4` response bodies were never persisted. Candidate 003's
  mechanism confidence is permanently MEDIUM and S3T.0 recovers it not at all.
- **One observation per cell.** S3S.1 §7.1 and §7.2 rest on one degenerate ceiling per
  arm. They are counted facts, not calibrated rates, and 36 synthetic tasks from one
  authoring process support no percentage claim.
- **`eval-v5` shares zero task instances with v2, v3 or v4.** A future measurement on it
  is not a head-to-head comparison with candidates 001-003. Each candidate stays
  comparable only against its own simultaneously-measured baseline.
- **`config_hash` and `plan_hash` are host-bound.** They bind `output_root_id`, runtime
  and hardware evidence, and are re-derived on the executing host rather than pinned here.
- **Measurement remains one host, CPU, one seed, one run per candidate.** No repeat, no
  second host, no GPU, no dtype control arm — and this design changes none of that.
- **Semantic leakage has never run.** `EXACT` and `LEXICAL` are clean; `SEMANTIC` is
  `NOT_QUALIFIED` and a pure paraphrase would not be caught.
- **D28, D29 and D33 bound any future measurement as they bounded every earlier one.**
  `tool_call_validity_rate` is vacuous so the six `tool_call_schema` tasks decide nothing,
  `timeout_rate` is structurally vacuous, and D29 bounds every refusal figure in both
  directions.
- **Gate thresholds remain uncalibrated.** `thresholds_are_calibrated` is false.
- **The single-axis proof covers the CONFIGURATION, not the trained weights.** It proves
  that exactly one input differs. It cannot prove that only one thing about the resulting
  model differs, and no such proof is available.

---

## 14 — Exact next step

**STOP.**

Candidate 004 is **designed** and **untrained**. Nothing about this document, this
configuration or generation 9 permits it to be trained.

The next milestone requires, in order and in a **fresh session**:

1. Read-only verification that the control plane is at generation 9 and PASSES.
2. A **new, explicit, single-use human TRAIN authorisation**. Nothing in this repository
   grants one, and the ruling recorded here does not carry forward.
3. Only then, a live training run of candidate 004 producing a portable training receipt,
   moving it `DESIGNED_UNTRAINED` → `TRAINED_UNEVALUATED`.

Evaluation is a separate, later, separately-authorised event. `eval-v5` stays
`FROZEN_UNUSED` until a candidate 004 adapter exists and a fresh human EVAL authorisation
is given at a new generation. Promotion is a third decision that no authority in this
repository grants.
