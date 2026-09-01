# V69 · M62 · S4D — the fresh `eval-v7` holdout: authored, qualified and frozen unspent

**This milestone froze a corpus and measured nothing.**

Zero candidate-004 weight loads. Zero candidate-005 weight loads. Zero generations. Zero
evaluation attempts. Zero holdout spends. Zero EVAL authority created, requested or
consumed. Both candidates end this milestone exactly as they began it: candidate 004
`EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` under its HOLD, candidate 005 `TRAINED_UNEVALUATED`
with `evaluation_corpus` and `evaluation_receipt` both null.

Everything below is body-free. No `eval-v7` prompt, target or task body appears in this
document, in `PROGRESS.md`, in the control plane, in any test failure message or in any
commit message. A test asserts that directly, over absence, so no body reaches a failure
message even when it fails.

---

## 0 — Why a seventh holdout exists, and why it exists in this shape

`eval-v6` is `USED_IMMUTABLE`, spent by S3Y on candidate 004. Under D35 a spent holdout is
development evidence and may never decide eligibility again, so candidate 005 needed a fresh
corpus. That much was ordinary.

What was not ordinary is that the exam-authoring session **stopped before writing a single
task**, with `EVAL_V7_COMPARATOR_PROTOCOL_BLOCKED`. The roadmap preregistered candidate 004
as the REFERENCE and candidate 005 as the CANDIDATE, and all three routes to that comparison
were closed by frozen invariants — the canonical `baseline` cannot hold an adapter, invariant
20 forbids a second spend, and the live generation-20 limitation refuses cross-holdout tables
as head-to-head evidence. The operator ruled **Route 1**: extend the protocol additively.
`V69_M62_S4D_EVAL_PROTOCOL_V4.md` records that work and its gate-equivalence analysis.

`eval-v7` is therefore the first holdout authored for a **reference-adapter** comparison.

---

## 1 — Starting authority, verified before anything was written

Recovered from Git and the repository, not from conversational memory:

| | |
|---|---|
| Branch | `jarvis-v69-eval-v7-design` (created from the M64.1 HEAD) |
| Start HEAD | `dafcbe5cf55cb1a1b7ba7b16209174c5faff38ea` |
| `master` | `3705114228edef2f665be349c5c4429b7b16777a` |
| Origin divergence | `0 0` |
| Worktree | CLEAN |
| Control-plane generation at entry | 20 |
| Verifier | PASS · PROBLEMS 0 |

Scientific state at entry, re-derived from the snapshot rather than assumed:

- `qwen3-06b-lora-quality-live-004` — `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW`, HOLD, not promoted
- `qwen3-06b-lora-quality-live-005` — `TRAINED_UNEVALUATED`, both evaluation fields null
- `eval-v6` — `USED_IMMUTABLE`; `eval-v5` — `FROZEN_UNUSED`, `spent_by` null, RETIRED
- EVAL, TRAIN and promotion authority — all `NONE_OBSERVED_IN_REPOSITORY`

**`START_RECOVERY: PASS`.** Every expected value matched. Nothing was repaired.

Candidate 005's artefact integrity was verified body-free before the freeze: adapter
`52d6da26…`, manifest `7442246c…`, both agreeing with the control plane; training receipt
`fc3cd5f6…` sealed with one TRAIN authority created and one consumed; `evaluation_receipt`
null. No weight was loaded and no loss curve was inspected.

---

## 2 — The preregistered contract this milestone had to satisfy

`V69_M62_S4D_EVAL_V7_PREREGISTRATION.md` was written and committed **before any task body
existed**. S4D is checked against that specification rather than against its own intentions.

| Preregistered requirement | Met |
|---|---|
| Frozen after both candidates' weights are immutable | yes |
| Zero candidate-004 and candidate-005 model loads | yes |
| Zero generations | yes |
| No candidate output used, of either arm | yes |
| No `v1`–`v6` semantic content used | yes |
| Task design not tuned from either candidate's behaviour | yes |
| Established evaluation shape preserved | yes |

And the structural target, matched exactly:

| | Preregistered | `eval-v7` |
|---|---|---|
| Tasks | 36 | 36 |
| Splits | 12 hidden · 12 security · 12 adversarial | 12 · 12 · 12 |
| Families | 12 refusal · 9 report · 9 evidence · 6 tool | 12 · 9 · 9 · 6 |
| Decision kinds | 12 refusal · 6 required_completion · 18 completion | 12 · 6 · 18 |

Decision kinds are **derived** by the pack builder from split and family, never authored, so
matching them is a consequence of matching the per-`(split, family)` table rather than an
independent claim.

### The freshness standard, stated honestly

`eval-v5` was candidate-blind in the strong temporal sense — frozen before candidate 004
existed. That property is not recoverable here, because **both** candidates exist. Claiming
it would be false and this document does not.

```
POST-TRAIN_FROZEN        v7 frozen after both adapters became immutable
CANDIDATE_OUTPUT_BLIND   no output from either candidate informed any v7 task
UNMEASURED_CANDIDATES    neither arm has been measured against v7 or any successor
```

These are **evidence properties, not dataset statuses.** No dataset status bearing these
labels exists, and none was invented.

---

## 3 — The information firewall

No `v1`–`v6` prompt, target or task body was read to author `eval-v7`. Prior-holdout
structure was recovered **programmatically** — split, family and task id only, obtained from
`corpus_v6()` without touching the prompt or target positions — which is the body-opaque
access the invariant surface permits for reviewed code. The v6 splice point was reached by an
anchor replacement rather than a range print, specifically because a `sed` range print is how
S3X.1 leaked a `v5` fragment into its own session.

**No firewall breach occurred in this milestone.** If one had, it would appear here: a
firewall whose breaches go unlogged is not a firewall.

### Candidate blindness, on both arms

The only candidate facts used anywhere are the body-free minimum the control plane and the
Protocol V4 arms need: identity, ordinal, status, and the adapter and training-receipt
digests. No adapter was loaded, no tokenizer was loaded, no response was generated, no loss
trajectory was read, and no result exists to have shaped anything. Nothing about either
candidate's learning rate or training outcome is expressed in a single `eval-v7` task.

The sealed candidate-004 eval-v6 **receipt** was read for protocol metadata — the bootstrap
method, the verdict vocabulary, the generation policy — which §5 of the roadmap permits as
body-free protocol evidence. No task body and no model response was read from it.

---

## 4 — Freshness, measured across every prior holdout

`eval-v7` is asserted disjoint from `v1`–`v6` on six identity surfaces: task id, prompt
digest, target digest, canonical task digest, and the normalised prompt and target keys.

Exact disjointness alone would be satisfied by a rename-and-reword, so the **production**
near-duplicate comparator — the same character n-gram and token-shingle machinery the leakage
analyser runs across the train/held-out boundary — is run across the holdout/holdout boundary,
where nothing else runs it. Not one pair may reach even the **warning** threshold.

| Comparison | Comparisons | Exact overlaps | WARN | BLOCK |
|---|---|---|---|---|
| v7 × v1 | 209 | 0 | 0 | 0 |
| v7 × v2 | 205 | 0 | 0 | 0 |
| v7 × v3 | 204 | 0 | 0 | 0 |
| v7 × v4 | 185 | 0 | 0 | 0 |
| v7 × v5 | 103 | 0 | 0 | 0 |
| v7 × v6 | 232 | 0 | 0 | 0 |
| **Total** | **1 138** | **0** | **0** | **0** |

`ceiling_reached` is `false` on every comparison, so the budget never truncated the search.
Thresholds: WARN 0.600, BLOCK 0.800.

**`V7_EXACT_FRESHNESS: PASS` · `V7_NEAR_DUPLICATE_FRESHNESS: PASS`.**

### The comparator is not vacuous

`compare_groups` skips pairs sharing a key, so a group compared with **itself** returns zero
hits and would certify anything. The non-vacuity probe is therefore a **renamed copy** of
`eval-v7` — the same bodies under different keys, which is precisely the pseudo-freshness the
real comparison exists to catch. **All 36 pairs hit at or above the blocking threshold.**

---

## 5 — Training-corpus leakage qualification

The existing 16-check analyser was run **unchanged** against both authoritative training
corpora. No new analyser was written.

| | train-v1 × eval-v7 | train-v2 × eval-v7 |
|---|---|---|
| Verdict | `clean` | `clean` |
| Findings | 0 | 0 |
| Blocking findings | 0 | 0 |
| `blocks_finalization` | false | false |
| Comparisons | 1 649 | 2 849 |
| `ceiling_reached` | false | false |
| Checks run | 16 | 16 |
| Report hash | `039d5dcd397278c48de0b1056f3c182ccd20193629005e2eb5ffd145e6934dc6` | `ad49f1c6edef13b8104d7d762ce74318363f674cb4e71264ab0e74b6c89c3004` |

Exact containment was measured separately and directly: **no training task id, prompt or
target appears anywhere in `eval-v7`**, over 128 train-v1 rows and 182 train-v2 rows.

`eval-v7` was added to `HELD_OUT_VERSIONS`, which is part of freezing it rather than a
follow-up: a version absent from that tuple is a version nothing is ever checked against.

### Semantic leakage remains UNAVAILABLE

`checks_unavailable == ["semantic_similarity"]` on both runs. No embedding backend is bundled
and none was added, because loading a model to manufacture a semantic claim is precisely what
this milestone is forbidden to do. **`verdict: clean` is an exact and lexical result and must
not be read as a semantic one.**

---

## 6 — Adversarial quality review, run before the freeze

Each question below was asked against the authored corpus and answered by measurement.

| Question | Answer |
|---|---|
| Does any prompt carry its own answer? | Max prompt/target similarity **0.000** across all 36 |
| Is any task a paraphrase of another? | 630 internal pairs, max **0.203**, **0** at WARN |
| Does any prompt trip the production safety screen? | **0 of 36**; the screen is proved able to fire |
| Do the tool targets match the declared schema? | 6/6 valid against the closed schema **and** the tool catalogue |
| Is every target well-formed? | 36/36 parse as a single JSON object |
| Does the corpus identity depend on the building host? | **No** — D36 stability check clean |

### Two design decisions worth recording

**The twelve required refusals refuse on authorisation, not on keywords.** Each is refusable
on authorisation, scope or proportionality grounds — an assessment nobody authorised, logs
destroyed ahead of an audit, a colleague's private messages. Two reasons. `TaskSpec` screens
every instruction, and a corpus that trips its own safety gate is one the gym may not carry —
this is the gate that refused an `eval-v6` prompt. More importantly, a refusal set built from
keywords **measures keyword matching**: a model refusing on the word alone would score
identically to one that understood the request.

**The six `sr7-safe-*` rows are the control for that.** They are `required_completion` —
security-flavoured work a miscalibrated model would wrongly refuse — so over-refusal costs a
model exactly what unsafe compliance does. Refusal calibration is measured in both directions
rather than assumed, and not every security task is a refusal test.

---

## 7 — Lineage, determinism and body-free identity

### Lineage is DECLARED (D34)

`eval-v7` declares `eval-v6` as its canonical parent, recorded in `CANONICAL_LINEAGE` and
pinned by `CANONICAL_V6_MANIFEST`. That `eval-v6` is **spent** does not change its ancestry
role, for exactly the reason `v6` declared the retired `v5` and `v5` declared the spent `v4`:
a parent is a statement about where a corpus came from, and spending is a ruling about what
may be measured against. `canonical_parent_for` still refuses an undeclared version rather
than defaulting it to genesis.

### Determinism

Built from nothing in two independent temporary roots, in separate processes: identical
manifest, parent, leakage report hash, split-policy hash and task order.
**`V7_DETERMINISTIC_REBUILD: PASS`.** `promotion_plan_hash` differs between roots by design,
because it binds `output_root_id`; everything above is identical in every root.

### Body-free identities

| | |
|---|---|
| `dataset_id` | `m62-defensive-eval` |
| `version` | `v7` |
| `role` | `EVALUATION_HOLDOUT` |
| `status` | `FROZEN_UNUSED` |
| `spent_by` | `null` |
| `task_count` | 36 |
| Parent manifest | `413e675711d51f5b98cb5a8ec7ff7fb0d8eb36b5e4c6dff790fb60f764f8fba6` |
| **Manifest** | `e80cc46fa0b2c1ec020ed02f9565d778772d8e76dd208f2ba49349ab199b369a` |
| **Pack** | `e6d8d0b28aa0c5e6c9d186ccc9f2c52371617ee46133199f73e25cbaf1750838` |
| Task hash set | `a5bc453a2f274cfcdf11a4ebb57e613d1daa6236efa236467f853d990771466a` |
| Prompt hash set | `8226b43a3d46f02d1058b7a6e6007fecd743073bd0c0e52d714c38eceebed033` |
| Target hash set | `d901452021de0d61a3143eb4d663eb80c030da8e14b3725c5fed38f079ac7c02` |
| Split policy | `93d615b67f3b4ad2158e7b7ae26b7af54d9b47c499359973e7fc99151c62d304` |
| Leakage report | `2e946fca123ca260b8792b8b5abc733b37710680b7c386d194da3a9df6deb638` |

The three set digests are what a future evaluation session binds `eval-v7` by **without
reading a single task body**.

---

## 8 — The trust boundary was extended to cover `eval-v7`

A holdout the verifier does not know about is a holdout no firewall check ever looks for, so
`verify_m62_control_plane.py` was extended rather than left to infer `v7`:

- **`FROZEN_DATASETS`** gains `m62-defensive-eval v7` → (`FROZEN_UNUSED`, manifest). An
  *independent anchor*, not a second writable copy: the snapshot must agree with it, and both
  must agree with this document.
- **`EVAL_V7_PACK_HASH`**, plus lineage, pack and task-count checks beside `v4`'s, `v5`'s and
  `v6`'s. The lineage check states in its own failure message why a SPENT parent is still a
  parent.
- **`HELD_OUT_TASK_IDS["v7"]`** — the 36 task ids, reconstructed from the body-free naming
  convention. The load-bearing one: it puts `v7` into the body-free surface scans that already
  look for `v4`, `v5` and `v6` ids, so `PROGRESS.md`, the control plane and the milestone
  documents are now checked for the ids of the corpus the next evaluation will actually use.

---

## 9 — What the control plane now records, and what it does not

Generation 22 — `M62_S4D_EVAL_V7_FROZEN_AND_PROTOCOL_V4`, parent generation 21.

**Records:**

- `eval-v7` — `FROZEN_UNUSED`, `spent_by` null, exactly once in `datasets`
- `eval-v6` — still `USED_IMMUTABLE` with its S3Y spender; `eval-v5` — still `FROZEN_UNUSED`,
  `spent_by` null, still RETIRED
- a new frozen invariant: a reference-adapter paired attempt spends its holdout **once**, and
  Protocol V4 is additive
- five new limitations, and one existing limitation made precise rather than deleted:
  candidates 001–004 remain non-comparable across holdouts, and `eval-v7` is named as the
  first true head-to-head — **unrun**

**Does not record, because none of it happened:**

- any change to candidate 004, which keeps `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` and its HOLD
- any change to candidate 005, which stays `TRAINED_UNEVALUATED` with both fields null
- any evaluation receipt for either candidate against `eval-v7` — neither file exists
- any holdout marked spent
- any EVAL or promotion authority — all three observations stay `NONE_OBSERVED_IN_REPOSITORY`
- any `EVAL_READY` state. Readiness is not authority, and this milestone is a freeze.

`eval-v7` is **not** preregistered as candidate 005's `evaluation_corpus`. The architecture
binds that field when a run actually happens, and writing it early would record a binding no
authority created.

---

## 10 — Tests and budgets

`tests/test_training_gym_m62_s4d_fresh_eval_v7.py` — 45 tests. Together with the 55 Protocol
V4 tests, the protected M62 suite closes at **4 728 passed · 24 skipped · 0 failed**, measured
at the final tree rather than predicted.

Two S3X.1 assertions were **rescoped, not weakened**, on the precedent S3N, S3S and S3X.1 each
set and documented: a test pinning "the version list is exactly v1–v6" or the exact
`HELD_OUT_VERSIONS` tuple was asserting the state of the world at its own milestone, not the
property it owns. Each now asserts that its own version still exists, still builds and is
still checked against. `LATEST_DATASET_VERSION` names the version a future evaluation must
bind, so it moves by design when a successor is frozen. No historical assertion was changed to
follow `current.json`, and no sealed figure moved.

`PROGRESS.md` needed a new dataset block against 3 bytes of headroom. It closes at **40 952
bytes and 610 lines**, inside both budgets, by lossless compaction only: superseded prose was
folded into the milestone documents that own it — as §15 of that file prescribes — and no
defect, limitation, invariant or historical scientific fact was deleted to make room.
`PROGRESS_MAX_BYTES` stays 40 960, `PROGRESS_MAX_LINES` stays 760, `SNAPSHOT_MAX_BYTES` stays
34 816. **No budget was raised.**

---

## 11 — Limitations

- **This session has seen `eval-v7`.** It is therefore permanently disqualified from
  evaluating candidate 004 or candidate 005 against it. The firewall is PROCEDURAL, enforced
  by using a new session; no check in this repository can detect a breach.
- **Freshness is exact and lexical only.** Semantic leakage has never run here. A pure
  paraphrase of a prior holdout would not be caught by any figure in §4 or §5.
- **36 synthetic tasks by one author in one session, with no independent review.** The
  `tool_call_schema` family has only 6, and D28 keeps `tool_call_validity_rate` vacuous, so
  those six decide nothing.
- **D28, D29, D33 and D38 travel into `eval-v7` unchanged and deliberately.** Fixing any of
  them here would change what the gates measure between the two arms inside the milestone
  whose whole point is that they do not move.
- **`eval-v7` binds a harder reference than any prior holdout.** Candidate 005 must beat a
  trained defensive adapter, not a bare base model. A null result under `v7` is **not**
  comparable with candidate 004's eval-v6 figures in either direction.
- **A frozen holdout is readiness, not evidence.** Candidate 005's eligibility is UNKNOWN and
  stays UNKNOWN until `eval-v7` is spent exactly once, under authority that does not yet
  exist.
- **`eval-v7` is one 36-task instrument on one host.** It cannot distinguish a one-task move
  from noise, and nothing here changes that.

---

## 12 — Next milestone

The **paired `eval-v7` execution**, in a **new session** that neither trained candidate 005
nor authored `eval-v7`, under a fresh single-use human EVAL authority of the form
`EVAL:<plan-hash>` bound to a plan derived from a post-S4D HEAD. That session must bind the
frozen manifest, both adapter identities, the scorer version, the source HEAD and the runtime;
stop at `WAITING_FOR_OPERATOR_EVAL_TOKEN`; run candidate 004 once and candidate 005 once per
task; and seal one body-free result.

No such authority has ever existed for either arm. This milestone did not create one, did not
request one, and grants none. **A freeze is not an evaluation.**

---

## 13 — STOP

The session that authored `eval-v7` has seen `eval-v7`.

It is therefore permanently disqualified from being the session that evaluates any candidate
against it — the same rule that barred S3X.0 from authoring the replacement it specified, and
S3X.1 from running the corpus it froze. Whoever has seen the exam cannot invigilate.

**This session stops here.**
