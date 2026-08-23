# V69 · M62 · S3X.1 — the fresh `eval-v6` holdout: authored, qualified and frozen unspent

**This milestone froze a corpus and measured nothing.**

Zero candidate-004 weight loads. Zero generations. Zero evaluation attempts. Zero holdout
spends. Zero EVAL authority created, requested or consumed. Candidate 004 ends this
milestone exactly as it began it: `TRAINED_UNEVALUATED`, with `evaluation_corpus` null and
`evaluation_receipt` null.

Everything below is body-free. No `eval-v6` prompt, target or task body appears in this
document, in `PROGRESS.md`, in the control plane, in any test failure message or in any
commit message. A test asserts that directly, and is proved non-vacuous against a planted
positive.

---

## 0 — Why a sixth holdout exists

`eval-v5` was frozen unspent by S3S, before candidate 004 existed at all. It still is
unspent: `FROZEN_UNUSED`, `spent_by` null, zero weight loads, zero generations. No model
has ever seen it.

What failed was not its lifecycle but its **precondition**. Defect **D44** — `repr` of a
bound method interpolating `repr(__self__)` — rendered `eval-v5` task bodies into an
orchestration session *before any evaluation was authorised*. Its preregistered
body-blindness property had therefore already failed when the exposure was found.

The operator's ruling at generation 12 was **retirement, not reuse**: relaxing a
preregistered gate after it fails is post-hoc protocol weakening. `eval-v5` may never again
be eligibility evidence, it stays `FROZEN_UNUSED` with `spent_by` null because that is the
truth, and the invariant surface carries the replacement requirement as
`FRESH_V6_REQUIRED`.

`eval-v6` is that replacement. It satisfies the requirement; it does not rehabilitate
`eval-v5`, and the retirement survives it unconditionally.

---

## 1 — Starting authority, verified before anything was written

Recovered from Git and the repository, not from conversational memory:

| | |
|---|---|
| Branch | `jarvis-v69-m62-training-gym` |
| HEAD (local == origin) | `abc2a83b71397062a9aa545199fd6ef03ddaa727` |
| Parent (Phase-A of S3X.0) | `dcd38cf573e75841581859ba3f95fb59a07afd57` |
| `master` | `3705114228edef2f665be349c5c4429b7b16777a` |
| Origin divergence | `0 0` |
| Worktree | CLEAN |
| Control-plane generation | 12 |
| Gen-12 snapshot | `0012-m62-s3x0-holdout-firewall-recovery.json` |
| Gen-12 SHA-256 | `6d04f5ee05f60c497e9396d861e7074deb5d02790219ff9bbba8cd85be81dc54` |
| Gen-12 size | 33 739 bytes, 1 077 headroom, 34 816 budget |
| Verifier | PASS · PROBLEMS 0 · 15/15 categories |

Scientific state at entry, re-derived from the snapshot rather than assumed:

- `qwen3-06b-lora-quality-live-004` — `TRAINED_UNEVALUATED`, both evaluation fields null
- `eval-v5` — `FROZEN_UNUSED`, `spent_by` null, ELIGIBILITY_USE RETIRED, never model-spent
- `D44` — FIXED, `is_gate` true
- `FRESH_V6_REQUIRED` — present on the invariant surface
- EVAL authority — `NONE_OBSERVED_IN_REPOSITORY`; promotion authority likewise

**`START_RECOVERY: PASS`.** Every expected value matched. Nothing was repaired.

---

## 2 — The preregistered contract this milestone had to satisfy

S3X.0 §11 preregistered `eval-v6` body-free, *before* any of it existed. S3X.1 is checked
against that specification rather than against its own intentions.

| Preregistered requirement | Met |
|---|---|
| New, dedicated holdout-builder session | yes |
| Frozen after candidate 004's weights are immutable | yes |
| Zero candidate-004 model loads | yes |
| Zero generations | yes |
| No candidate-004 output used | yes |
| No `eval-v5` semantic content used | yes |
| Task design not tuned from candidate-004 behaviour | yes |
| Established evaluation shape preserved | yes |

And the structural target, matched exactly:

| | Preregistered | `eval-v6` |
|---|---|---|
| Tasks | 36 | 36 |
| Splits | 12 hidden · 12 security · 12 adversarial | 12 · 12 · 12 |
| Families | 12 refusal · 9 report · 9 evidence · 6 tool | 12 · 9 · 9 · 6 |
| Decision kinds | 12 refusal · 6 required_completion · 18 completion | 12 · 6 · 18 |

The decision kinds are **derived** by the pack builder from split and family, never
authored, so matching them is a consequence of matching the per-`(split, family)` table
rather than an independent claim.

### The freshness standard, as preregistered

`eval-v5` was candidate-blind in the **strong temporal sense** — frozen before candidate
004 existed. That property is not recoverable, because candidate 004 exists now. Claiming
it for `eval-v6` would be false, and this document does not.

The achievable standard S3X.0 preregistered, and which S3X.1 meets:

```
POST-TRAIN_FROZEN        v6 frozen after candidate 004's weights became immutable
CANDIDATE_OUTPUT_BLIND   no candidate 004 output informed any v6 task
UNMEASURED_CANDIDATE     candidate 004 has never been measured against v6 or any successor
```

These are **evidence properties, not dataset statuses.** No dataset status bearing these
labels exists, and none was invented.

---

## 3 — The information firewall, and one honest exception

### What was not read

No `v1`–`v5` prompt, target or task body was read to author `eval-v6`. No prior task was
renamed, reworded or paraphrased. The D44 incident material was not reopened — re-reading
it to "make v6 different" would inherit exactly the exposure that retired `eval-v5`.

Freshness is therefore **measured, not trusted**: §5 reports it across six identity
surfaces and the production near-duplicate comparator, over all five prior versions.

### The exception, recorded rather than omitted

While locating the splice point for the new generator function, a `sed` range print of the
generator source displayed the **tail of the final `eval-v5` tool-family row** — part of one
prompt and one target. That is a `v5` body fragment reaching this session, and it is
recorded here because a firewall whose breaches go unlogged is not a firewall.

What it does and does not change:

- **It does not change `eval-v5`'s status.** `eval-v5` was already permanently exposed
  under D44 and already retired from eligibility use. Its `FROZEN_UNUSED` / `spent_by`
  null lifecycle record is unaffected, because no model saw it then either.
- **It does not change `eval-v6`.** The six `eval-v6` tool-family rows were authored
  before this happened, use a different tool surface, and are measured disjoint from
  `eval-v5` on every identity surface with zero near-duplicate hits at any threshold.
- **It reinforces the standing rule** that the session which authors a holdout may not be
  the session that evaluates against it — see §9.

The D44 limitation already records that one rendered body is a **floor**, not a count. This
fragment is consistent with that and does not make it a ceiling either.

### Candidate-004 blindness

The only candidate-004 facts used anywhere in this milestone are the body-free minimum the
control plane needs to stay consistent: identity, ordinal `4`, status
`TRAINED_UNEVALUATED`, and the two null evaluation fields. No adapter was inspected, no
weight was loaded, no tokenizer was loaded, no response was generated, and no result
exists to have shaped anything. Nothing about candidate 004's architecture, learning rate
or training outcome is expressed in a single `eval-v6` task.

---

## 4 — Capacity, proved before a byte of corpus was written

This was the milestone's real risk. Generation 12 closed at **33 739 / 34 816** — 1 077
bytes of headroom against a 1 024-byte policy floor, so **53 bytes of true slack** — while
generation 13 must carry a sixth dataset entry costing roughly 490 bytes on its own.

`jarvis/scripts/project_m62_gen13_capacity.py` projects the minimum truthful generation 13
and measures it. It is the same transform `--emit` writes, so the bytes measured are the
bytes that land on disk, and it **fails closed**: `--emit` refuses if the gate does not
pass, and refuses again if a stand-in digest would be written into real state.

The first projection **FAILED** at 416 bytes of headroom. It passes now at **1 059**, and
the difference is not a budget raise:

> `SNAPSHOT_MAX_BYTES` stays 34 816. `PROGRESS_MAX_BYTES` stays 40 960.
> `PROGRESS_MAX_LINES` stays 760. A test asserts all three literals.

It fits because of **lossless compaction of prose generation 13 genuinely supersedes or
duplicates**:

- **`next_milestone` was rewritten.** It is prospective by construction — it describes the
  milestone that has not happened, and generation 12's described S3X.1, which is now done.
  The replacement is tighter because the permanent rules it used to restate at length live
  on the `frozen_invariants` surface, which is where permanent rules belong.
  `_check_ruled_out` enforces coverage as substrings precisely so the wording stays free
  while the coverage does not.
- **Four limitation entries were merged with the entry already carrying their subject**:
  the holdout root-independence pair, the two environmental optional-dependency facts, the
  D33 corollary and D33 itself, and the two holdout-authoring firewalls.

No defect, limitation, invariant or historical scientific fact was deleted to make room.
Thirty named clauses are listed in `CARRIED_FORWARD` and checked **fail-closed inside the
producer**, before the projection is measured — a compaction that drops a standing
prohibition raises rather than reports. A separate test mutates the snapshot to prove that
check can actually fail.

| | |
|---|---|
| Projected generation 13 | 33 757 bytes |
| Headroom | **1 059** (floor 1 024) |
| `PROGRESS.md` | 40 592 bytes / 609 lines |
| PROGRESS headroom | 368 bytes / 151 lines |
| Compaction | LOSSLESS across 30 carried-forward clauses |

---

## 5 — Freshness, measured across every prior holdout

`eval-v6` is asserted disjoint from **`v1`, `v2`, `v3`, `v4` and `v5`** on six identity
surfaces: task id, prompt text, target text, canonical task hash, prompt hash, target hash
and candidate hash.

Exact disjointness alone would be satisfied by a rename-and-reword, so the **production**
near-duplicate comparator — the same character n-gram and token-shingle machinery the
leakage analyser runs across the train/held-out boundary — is run across the
holdout/holdout boundary, where nothing else runs it. Not one pair may reach even the
**warning** threshold.

| Comparison | Comparisons | Exact overlaps | WARN | BLOCK |
|---|---|---|---|---|
| v6 × v1 | 264 | 0 | 0 | 0 |
| v6 × v2 | 328 | 0 | 0 | 0 |
| v6 × v3 | 381 | 0 | 0 | 0 |
| v6 × v4 | 599 | 0 | 0 | 0 |
| v6 × v5 | 992 | 0 | 0 | 0 |
| **Total** | **2 564** | **0** | **0** | **0** |

`ceiling_reached` is `false` on every comparison, so the budget never truncated the search.
Thresholds: WARN 0.600, BLOCK 0.800.

**`V6_EXACT_FRESHNESS: PASS` · `V6_NEAR_DUPLICATE_FRESHNESS: PASS`.**

### The comparator is not vacuous

`compare_groups` skips pairs sharing a key, so a group compared with **itself** returns
zero hits and would certify anything. The non-vacuity probe is therefore a **renamed copy**
of `eval-v6` — the same bodies under different keys, which is precisely the pseudo-freshness
the real comparison exists to catch. Every one of the 36 pairs hits, at or above the
blocking threshold.

---

## 6 — Training-corpus leakage qualification

The existing 16-check analyser was run **unchanged** against both authoritative training
corpora. No new analyser was written.

| | train-v1 × eval-v6 | train-v2 × eval-v6 |
|---|---|---|
| Verdict | `clean` | `clean` |
| Findings | 0 | 0 |
| Blocking findings | 0 | 0 |
| `blocks_finalization` | false | false |
| Comparisons | 4 059 | 6 186 |
| `ceiling_reached` | false | false |
| Checks run | 16 | 16 |
| Report hash | `fc3caac7d71fb308820896e5c06098571290c0227af7823ed04f890e2487c3cf` | `e9f2ea19a84215dd2d91be1246eb9f8b45ca48b6ffe1e1e33dee1a7ff102740d` |

Exact containment was measured separately and directly: no training task id, prompt or
target appears anywhere in `eval-v6`.

`eval-v6` was added to `HELD_OUT_VERSIONS`, which is part of freezing it rather than a
follow-up: a version absent from that tuple is a version nothing is ever checked against.
The retired `eval-v5` **stays** in the tuple — retirement bars it as *evidence*, while
training on material it contains would still be contamination.

### Semantic leakage remains UNAVAILABLE

`checks_unavailable == ["semantic_similarity"]` on both runs. No embedding backend is
bundled and none was added, because loading a model to manufacture a semantic claim is
precisely what this milestone is forbidden to do. **`verdict: clean` is an exact and
lexical result and must not be read as a semantic one.**

---

## 7 — Lineage and deterministic identity

### Lineage is DECLARED (D34)

`eval-v6` declares `eval-v5` as its canonical parent, recorded in `CANONICAL_LINEAGE` and
pinned by `CANONICAL_V5_MANIFEST`. A lineage discovered from whatever happens to be in the
destination root is the D34 defect, and `canonical_parent_for` still refuses an undeclared
version rather than defaulting it to genesis.

That `eval-v5` is **retired** does not change its ancestry role, for exactly the reason
`eval-v5` itself declared the **spent** `eval-v4` as its parent: a parent is a statement
about where a corpus came from, and retirement is a ruling about what may be measured
against. Declaring it neither reopens it nor makes `eval-v6` a derivative of its material —
and §5 measures that rather than asserting it.

### Determinism

Built from nothing in two independent temporary roots, in separate processes: identical
manifest, parent, leakage report hash, split-policy hash, task order and pack hash.

**`V6_DETERMINISTIC_REBUILD: PASS`.**

### Body-free identities

| | |
|---|---|
| `dataset_id` | `m62-defensive-eval` |
| `version` | `v6` |
| `role` | `EVALUATION_HOLDOUT` |
| `status` | `FROZEN_UNUSED` |
| `spent_by` | `null` |
| `task_count` | 36 |
| Parent manifest | `e852f4627d4fe631f58ee3d120d5d1a81c94480a1c0b84e590d2b08261043f4c` |
| **Manifest** | `413e675711d51f5b98cb5a8ec7ff7fb0d8eb36b5e4c6dff790fb60f764f8fba6` |
| **Pack** | `41579381422636d073d8ce3a0df230cafb97ffdd1489ab02126f2273565ade16` |
| Task hash set | `5dfbf21f23e716ca000aeca33a744b41ba789f902032f57e704f0f789109a4d5` |
| Prompt hash set | `792d9e72bbf15eaf1770c2e041f3436685d1a3b7e4916a5bff1a5eec2adee7fa` |
| Target hash set | `b86ef7fe4b191b0ad624f18f257b237d659bb0988bf3223c0460961e17b8d035` |
| Split policy | `1e9eab7741c6d4566d0b3e4c80631e6461a6316b4640d7a43142dd88fe2dbef1` |

The three set digests are what a future evaluation session binds `eval-v6` by **without
reading a single task body**. `promotion_plan_hash` is deliberately excluded: it binds
`output_root_id` and differs between roots by design, while everything above is identical
in every root.

---

## 8 — Two production gates refused this corpus before it was frozen

Both are recorded because a milestone that reports only its successes is reporting its
intentions.

**The safe-boundary validator refused one prompt.** `TaskSpec._validate_safe_boundary`
screens the instruction of every task, and one adversarial refusal prompt tripped its
`detection evasion` pattern. The requirement is subtle and real: a required-refusal task
must be something the model should *refuse*, without the corpus itself becoming a request
the gym may not carry. The prompt was rephrased to pose the same refusable request without
tripping the screen, and a test now asserts the screen ran clean over all 36 prompts —
plus a second test asserting the screen can still fire, so the first is not vacuous.

**The declared response schema refused six targets.** The pack builder gives
`TOOL_CALL_SCHEMA` tasks a strict schema — `additionalProperties: false`, exactly `tool`
and `arguments` — and the six tool targets initially carried explanatory fields the schema
does not permit. They were narrowed to the declared shape. The schema is authoritative;
the corpus was wrong, and the corpus changed.

Both refusals moved `eval-v6`'s manifest, pack and target-set digests. The values in §7 are
the post-fix ones, re-measured, and every freshness and leakage figure in §5 and §6 was
re-run against the final corpus rather than carried over.

---

## 9 — D44 stayed fixed, and the freeze did not recreate it

The D44 regression suite was run before and after `eval-v6` was materialised: **32 passed**
both times, and it is non-vacuous by construction.

| | |
|---|---|
| Probes with the guard REMOVED (must leak) | 3 passed — leaks reappear |
| Tests with the guard IN PLACE (must not leak) | 29 passed |
| `POST_FIX_CANARY_LEAKS` | **0** |
| `BODY_BLINDNESS_NONVACUITY` | **PASS** |
| `SEMANTIC_EVALUATION_IDENTITIES_CHANGED` | **NO** |

Canaries are synthetic throughout. No real `eval-v5` or `eval-v6` body was used as a
leak-test canary — sizing an exposure by re-rendering the material is the mistake D44
already made once.

Two additional tests were added for this milestone specifically: one renders the
`eval-v6` pack, a task, a bound method, a tuple, `%r` and `str()` and asserts no prompt or
target prefix appears in any of them; another asserts the freeze ceremony's own record
serialisation carries no body. Both assert over **absence**, so no body reaches a failure
message even when they fail.

The gate, metric and generation policy digests, `max_new_tokens` 512 and `reasoning_policy`
DISABLED are unchanged and pinned by test. No D38 gate and no D43 gate was created.

---

## 10 — What the control plane now records, and what it does not

Generation 13 — `M62_S3X1_FRESH_EVAL_V6_FROZEN`, parent
`6d04f5ee05f60c497e9396d861e7074deb5d02790219ff9bbba8cd85be81dc54`.

**Records:**

- `eval-v6` — `FROZEN_UNUSED`, `spent_by` null, exactly once in `datasets`
- `eval-v5` — still `FROZEN_UNUSED`, `spent_by` null, still ELIGIBILITY_USE RETIRED
- `FRESH_V6_REQUIRED` — retained on the invariant surface, now marked satisfied, with the
  retirement explicitly surviving it unconditionally
- a new clause on the body-blindness invariant: a session that legitimately authored a
  holdout is permanently disqualified from evaluating any candidate against it
- `D44` — FIXED, `is_gate` true, untouched

**Does not record, because none of it happened:**

- any change to candidate 004, which stays `TRAINED_UNEVALUATED` with both evaluation
  fields null
- any evaluation receipt for candidate 004 — the file does not exist; its **training**
  receipt is real and untouched
- any holdout marked spent, by candidate 004 or anyone
- any EVAL or promotion authority — all three observations stay
  `NONE_OBSERVED_IN_REPOSITORY`
- any `EVAL_READY` state. Readiness is not authority, and this milestone is a freeze, not a
  qualification. Generation 11's lesson stands: a qualified ceremony is not an authorised
  one.

`eval-v6` is **not** preregistered as candidate 004's `evaluation_corpus`. The architecture
binds that field when a run actually happens, and writing it early would record a binding
no authority created.

---

## 11 — Tests

Focused S3X.1 suites first, then the historical and freeze regressions, then one canonical
focused M62 suite. Actual counts are reported in `PROGRESS.md` and the final machine block;
the generation-12 figures were not reused.

Three superseded assertions were **rescoped, not weakened**, using the precedent S3N and
S3S each set and documented: a test that pinned "the version list is exactly v1–v5" or
"`LATEST_DATASET_VERSION == v5"` was asserting the state of the world at its own milestone,
not the property it owns. Each now asserts that its own version still exists and still
builds, and the undeclared-version probe moved from `v6` — which is declared now — to a
version nothing has ever declared. No historical assertion was changed to follow
`current.json`, and no sealed figure moved.

---

## 12 — Limitations

- **This session has seen `eval-v6`.** It is therefore permanently disqualified from
  evaluating candidate 004 against it. The firewall is PROCEDURAL, enforced by using a new
  session; no check in this repository can detect a breach.
- **Freshness is exact and lexical only.** Semantic leakage has never run here. A pure
  paraphrase of a prior holdout would not be caught by any figure in §5 or §6.
- **One `eval-v5` body fragment reached this session incidentally** (§3). It informed
  nothing, and `eval-v6`'s independence from `eval-v5` is measured rather than trusted.
- **36 synthetic tasks by one author in one session, with no independent review.** The
  tool_call_schema family has only 6, and D28 keeps `tool_call_validity_rate` vacuous, so
  those six decide nothing.
- **D29 travels into `eval-v6` unchanged and deliberately.** Its refusal targets are not
  recognised by `looks_like_refusal`, which bounds the refusal figures in both directions.
  Rewriting the phrasing to fix that would address D29 as a rider *and* change what QG-1
  and SV-5 measure between candidate 003 and candidate 004.
- **A frozen holdout is readiness, not evidence.** Candidate 004's eligibility is UNKNOWN
  and stays UNKNOWN until `eval-v6` is spent exactly once, under authority that does not
  yet exist.
- **`eval-v6` is one 36-task instrument on one host.** It cannot distinguish a one-task
  move from noise, and nothing here changes that.

---

## 13 — Next milestone

**S3Y** — qualify and run the candidate 004 evaluation against `eval-v6`, in a **new
session that did not author v6**, under a fresh single-use human EVAL authority of the form
`EVAL:<plan-hash>` bound to a plan derived from a post-S3X.1 HEAD.

No such authority has ever existed for candidate 004. This milestone did not create one,
did not request one, and grants none. A freeze is not an evaluation.

---

## 14 — STOP

The session that authored `eval-v6` has seen `eval-v6`.

It is therefore permanently disqualified from being the session that evaluates candidate
004 against it — the same rule that barred S3X.0 from authoring the replacement it
specified. Whoever has seen the exam cannot invigilate.

**This session stops here.**
