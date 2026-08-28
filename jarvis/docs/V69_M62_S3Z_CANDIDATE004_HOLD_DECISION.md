# V69 M62 S3Z — candidate 004: the human decision, and what it does not do

**HUMAN DECISION: HOLD.**

This document records a **governance** decision, not a measurement and not a
re-interpretation of one. S3Y answered the only question it was authorised to ask —
*did candidate 004 clear the frozen eligibility bar?* — and the answer was **yes**.
S3Z answers a different question, which no measurement can answer: *what should be done
about that?* The operator's answer is **HOLD**.

Zero model weight loads. Zero generations. Zero evaluation attempts. Zero holdout spend
events. Zero `TRAIN`, `EVAL` or promotion authority created, requested or consumed.
Nothing was promoted, activated, merged, tagged or released.

**No held-out task body, prompt, target, response or task identifier appears anywhere in
this document.** Every figure below is an aggregate or a digest, taken from the sealed
portable receipt; nothing was re-opened to write it.

---

## 1. The decision

| | |
|---|---|
| Decision | **HOLD** |
| Subject | `qwen3-06b-lora-quality-live-004` |
| Decided by | the human operator, carried into a fresh session |
| Candidate status | **unchanged** — `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` |
| Promoted | **NO** |
| Measurement | **accepted as valid**, and left byte-identical |
| Authority created | **NONE** — `HOLD` is not an executable capability |

**HOLD is not a candidate state.** The candidate state machine is unchanged: `HOLD`,
`HELD`, `ON_HOLD`, `DEFERRED` and `REJECTED_BY_OPERATOR` were **not** added to
`CANDIDATE_STATES`, no transition was added or relaxed, and no guard was touched. The
only transition still legal out of `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` remains
`-> PROMOTED` under `HUMAN_PROMOTION_AUTHORITY`, which **does not exist**. HOLD is
recorded on the control plane's *governance* surfaces — `control_plane_note`,
`next_milestone` and this document — precisely because it is not a scientific state.

### What HOLD means

* the S3Y measurement is **accepted** as valid evidence;
* candidate 004 is **preserved unchanged**;
* it is **not promoted**, not activated, not deployed;
* the measurement is **not rejected** either;
* **no further holdout may be spent**;
* model development **does not continue automatically**;
* the next model-facing step awaits a **separate** operator roadmap ruling.

### Why HOLD rather than PROMOTE

Eligibility passed, but general superiority was **not demonstrated** and a regression is
**not excluded**.

### Why HOLD rather than REJECT

The measurement is valid, every preregistered deterministic gate passed, and there were
**zero** security regressions.

---

## 2. What S3Z verified before recording anything

A decision recorded on top of unverified evidence is not governance. Both audits were
run **body-blind**, read-only, in a fresh session that inherited no procedural authority
from the S3Y evaluation session, and the decision was recorded only after both passed.

### 2.1 Immutable evidence-chain audit — PASS

The body-free chain was reconstructed from Git and tracked portable evidence rather than
taken from prose: the evaluation source commit, the measurement witness, the evidence
seal and the generation-14 control-plane commit, in that order and with the expected
ancestry; `master` unmoved. Candidate identity, adapter digest, training receipt and the
immutable base revision match exactly. The portable evaluation receipt verifies under
canonical production verification with **0 problems**, and independently binds the
candidate, the adapter, the training receipt, the base revision, the `eval-v6` manifest
and pack, the evaluation config, plan and report identities, the evaluation source
commit, the witness and the measurement result.

Single-use semantics hold: **1** evaluation attempt, **1** holdout spend event, **1**
plan consumption, `retry_authorized` **false**, `token_literal_recorded` **false**.
Measurement completeness holds: **36** paired tasks, **0** missing, terminal state
`completed`, no durability failure, no quarantine. Eligibility re-derives as
`eligible_for_human_review` with `blocking_count` **0** and `security_blocking_count`
**0**. Lifecycle is as sealed, and no edge leads back out of `USED_IMMUTABLE`.

### 2.2 Post-result change audit — PASS

Tracked test and control-plane code **was** changed after the S3Y result was known, so
the complete diff from the final pre-measurement plan source through generation 14 was
audited for post-hoc weakening, with pre-measurement changes, measurement evidence and
post-measurement changes classified separately.

Every post-measurement change mapped to a legitimate structural class: a sealed-generation
pin, a superseded absence assertion, a non-vacuity preservation, a strengthening, or a
synthetic-fixture isolation. Explicitly zero: eligibility thresholds changed, gate
thresholds changed, graders changed, metric-policy changes, generation-policy changes,
security-policy changes, candidate- or dataset-transition weakenings, receipt weakenings,
verifier production weakenings, invariants deleted, holdout-firewall weakenings, failing
tests deleted, `xfail`/`skip` added to hide a failure, and vacuous mutations introduced.

**Nothing that would have counted as PASS during the evaluation was changed afterwards.**

---

## 3. The sealed result, stated honestly

Taken from the portable receipt. Nothing here was re-derived by re-opening the corpus.

| Verdict | Count |
|---|---|
| improved | **9** |
| regressed | **14** |
| unchanged | **10** |
| security improvement | **3** |
| security regression | **0** |
| not comparable | 0 |

| | |
|---|---|
| paired tasks | **36** measured, **0** missing |
| paired mean delta | **+0.0751** |
| 95% interval | **[−0.0040, +0.1793]** |
| warning | **`regression_not_excluded`** |
| `blocking_count` | **0** |
| `security_blocking_count` | **0** |
| eligibility | `eligible_for_human_review` |

### The only defensible reading

* candidate 004 **cleared every preregistered deterministic eligibility gate**;
* it caused **zero measured security regressions**;
* it produced **three measured security improvements**;
* it **did not establish general quality superiority**;
* **ordinary task regressions outnumbered improvements**, 14 to 9;
* the confidence interval **does not exclude a regression**;
* the experiment **does not establish that the learning-rate change 1e-4 -> 5e-5 was
  beneficial**;
* `eval-v6` is **spent** and cannot answer the question again.

Read the counts before the verdict. "Eligible" here means *cleared a frozen, uncalibrated
36-task bar*, and nothing more. It is not production-readiness, not a promotion, and not
permission to seek one.

---

## 4. What remains true, and untouched

| Surface | State |
|---|---|
| candidate 004 | `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` — **unchanged** |
| candidate 004 adapter | `a105e01c…a1c3ecc67` — unchanged |
| evaluation corpus | `m62-defensive-eval v6` |
| evaluation receipt | `state/m62/receipts/qwen3-06b-lora-quality-live-004.eval.json` — **byte-identical** |
| S3Y measurement witness | **byte-identical** |
| plan / report identity | unchanged |
| measurement aggregates | unchanged |
| gates, graders, thresholds | unchanged |
| `eval-v6` | **`USED_IMMUTABLE`**, spent once by S3Y on candidate 004 |
| `eval-v5` | **`FROZEN_UNUSED`**, `spent_by` **null**, **RETIRED** from eligibility use |
| `eval-v4` | spent; development evidence under D35 |
| D44 | unchanged, `FIXED`, `is_gate` true |
| defect ledger | unchanged — **no D45 was created** |
| `master` | `3705114228edef2f665be349c5c4429b7b16777a` — untouched |

**No second look is permitted.** `eval-v6` is `USED_IMMUTABLE` and single-use: no rerun,
no re-score, no alternate seed, no partial replay, no ablation, no manual task
inspection, no qualitative sample review, no threshold experimentation, no different
scorer, grader or model arm, no further confidence interval, no cherry-picking. A spent
holdout is not a debugging instrument, and a result someone dislikes is not permission to
look again.

**No fresh eligibility corpus exists.** `eval-v4` and `eval-v6` are spent; `eval-v5` is
retired unspent and may never be eligibility evidence.

---

## 5. What S3Z does NOT authorise

`HOLD` creates **no executable authority**. `PROMOTION_AUTHORITY`, `TRAIN_AUTHORITY` and
`EVAL_AUTHORITY` are all **NONE**. The S3Y `EVAL` authority is spent and survives only as
historical evidence. No `EVAL:<…>`, `TRAIN:<…>`, `PROMOTE:<…>` or `HOLD:<…>` token was
created, and no tracked file carries a reusable authority token.

Explicitly **NOT** authorised by this decision:

* promotion, activation, deployment, registry mutation, merge, tag, release, version bump
* candidate 005 — not authorised, not designed, not named, not configured
* `eval-v7` or any replacement holdout — not authorised
* further training of any candidate — not authorised
* further evaluation of any candidate — not authorised
* retraining, resuming, re-seeding or patching candidate 004
* any change to learning rate, rank, alpha, dropout, modules or epochs
* any second look at `eval-v6`, or any access to `eval-v4`/`eval-v5` bodies

**PROSE_CANNOT_GRANT_AUTHORITY.** Neither this document nor the control plane can
authorise any of the above. Only a separate, explicit human ruling can.

---

## 6. Next

**The M62 model-development loop is PAUSED.**

Candidate 004 is retained as valid evaluated evidence. No model-facing continuation is
authorised, and none is implied by the eligibility result. The next model-facing action
requires a **separate explicit human roadmap ruling** — which this decision deliberately
does not make, and does not pre-authorise.
