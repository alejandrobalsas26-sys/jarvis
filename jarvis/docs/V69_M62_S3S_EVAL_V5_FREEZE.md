# V69 · M62 · S3S — fresh held-out evaluation `v5`: candidate-blind design, qualification and freeze

> **Status: `EVAL_V5_STATUS: FROZEN_UNUSED`.** A fifth held-out eligibility corpus was
> authored candidate-blind, qualified against the frozen evaluation contract, checked for
> freshness and leakage, rebuilt deterministically in three independent roots, and frozen
> — **before candidate 004 exists in any form**.
> **Zero training, zero evaluation, zero model loads, zero generations, no `TRAIN` or
> `EVAL` authority, no candidate 004 identifier, no candidate 004 configuration, no
> `train-v3`, and no ruling on candidate 004's axis.**

| | |
|---|---|
| Milestone | V69 M62 **S3S** — `m62-defensive-eval v5` only |
| Date | 2026-08-21 |
| Branch | `jarvis-v69-m62-training-gym` |
| Starting HEAD | `f9d25fd2a9f6ebe5b0ee7cdb487c21e368afc9b3` (S3R closure) |
| Master | `3705114228edef2f665be349c5c4429b7b16777a` (untouched) |
| Control plane | generation **6** in, generation **7** out |
| Preceding milestones | S3Q — `V69_M62_S3Q_CANDIDATE003_LIVE_HELDOUT_EVALUATION.md` · S3Q.0.2 — `V69_M62_S3Q02_SEAL_RECOVERY.md` · S3R — `V69_M62_S3R_CANDIDATE003_POSTMORTEM.md` |

**This document is body-free by construction.** It carries dataset identities, digests,
counts, matrices and provenance. It carries **no prompt, no target, no task body, no
hidden-target content and no shingle of any of them** — a property that is measured, not
promised: `test_the_body_free_surfaces_carry_no_v5_material` refuses any whole prompt,
any whole target and any eight-word shingle of either in this file, in `PROGRESS.md` and
in `state/m62/current.json`.

---

## 0 — The rule this milestone exists to enforce

> **Freeze the exam before building the student — and do not let the same session do both.**

`eval-v4` is `USED_IMMUTABLE`. S3Q spent it on candidate 003 and S3R then drew a body-free
termination diagnosis from its per-task results, which under operator ruling **D35** makes
it *development evidence*: it may inform design and may never decide eligibility again. A
fourth candidate therefore needs a **fifth** holdout, and that holdout must be frozen while
there is still no candidate 004 whose configuration, weights or outputs could have shaped
it.

S3S is deliberately stricter than "freeze the holdout before training". This session
**authored** the `v5` task bodies, so it has seen them; it is therefore forbidden to design
the model that will be tested on them. It freezes `v5`, seals generation 7, and stops.

Measured rather than asserted:

```
MODEL_WEIGHTS_LOADED:                  NO   (no tokenizer either)
MODEL_GENERATIONS:                     0
MODEL_RESPONSE_TOKENS_GENERATED:       0
OPTIMIZER_STEPS:                       0
NEW_EVAL_ATTEMPTS:                     0
TRAIN_ATTEMPTS:                        0
TRAIN_TOKEN_CREATED / CONSUMED:        NO / NO
EVAL_TOKEN_CREATED / CONSUMED:         NO / NO
ADAPTERS_CREATED / MUTATED:            0 / 0
CANDIDATE_004_CREATED:                 NO
CANDIDATE_004_IDENTIFIER_ASSIGNED:     NO
CANDIDATE_004_PRIMARY_AXIS_SELECTED:   NO
CANDIDATE_004_CONFIG_HASH:             null
TRAIN_V3_CREATED:                      NO
SPENT_V4_RESPONSE_BODIES_READ:         0
SPENT_V4_TASK_BODIES_READ:             0
GATES / GRADERS / THRESHOLDS:          UNCHANGED
```

**Sealed and not reopened:** the S3I, S3L and S3Q verdicts, candidates 001, 002 and 003,
the S3R diagnosis, and D28, D29, D33, D34, D35, D36, D37, D38, D39, D40, D41 and D42. All
three candidates remain `EVALUATED_NOT_ELIGIBLE`.

---

## 1 — Starting authority, verified before anything was written

| Fact | Expected | Observed |
|---|---|---|
| Branch | `jarvis-v69-m62-training-gym` | matches |
| HEAD | `f9d25fd2a9f6ebe5b0ee7cdb487c21e368afc9b3` | matches |
| `origin` feature branch | same as HEAD | matches |
| Divergence | `0  0` | `0  0` |
| `master` | `3705114228edef2f665be349c5c4429b7b16777a` | matches |
| Worktree | CLEAN | CLEAN |
| Schema | `m62.control_plane.1` | matches |
| Generation | 6 | 6 |
| Snapshot | `state/m62/snapshots/0006-m62-s3q-live-measurement-sealed.json` | matches |
| Snapshot SHA256 | `26f4ec17…ac96` | `26f4ec179a0b5ee7bbfc7b7487aa9ef9b5d4bdaec645e970fc3d523899ac1b96` |
| Subject commit | `7cc6d2674fc717f1f5da728e0ed12d47c6523bb1` | matches |
| candidate 003 | `EVALUATED_NOT_ELIGIBLE` on `eval-v4` | matches |
| `eval-v4` | `USED_IMMUTABLE` | matches |
| candidate 004 | `NOT_CREATED` | absent from the snapshot entirely |
| `eval-v5` | `NOT_CREATED` | absent |

`python jarvis/scripts/verify_m62_control_plane.py` → **PASS**, `PROBLEMS: 0`, all sixteen
sections PASS. **Nothing was repaired.**

---

## 2 — S3R is development evidence, not a content specification

S3R established, body-free, that candidate 003 carried a general stopping pressure — 6 of
36 responses at the 512-token output ceiling against the baseline's 2 — that its single
structured failure was a JSON parse failure at that ceiling, that every training target
fits comfortably below 512 (longest structured completion 91 tokens), and that D37 render
parity holds.

**None of that shaped one byte of `v5`.** Turning a diagnosis into a corpus would produce
"a test designed to make a particular candidate win", which is not a holdout. Specifically
NOT done:

* the `(split, family)` distribution was **not** re-weighted towards `structured_report`;
* no reference target was shortened, simplified or made easier to terminate;
* no schema was made shallower, stricter or looser;
* no prompt was written to reward early stopping;
* `max_new_tokens` was **not** raised — it stays 512, pinned by test;
* no task difficulty was tuned against any measured score;
* the refusal set was not enlarged, and the safe set was not enlarged.

`v5` continues the existing M62 defensive evaluation blueprint, authored from the family
contracts and split purposes exactly as `v3` and `v4` were.

---

## 3 — Candidate-blindness, stated as a constraint S3S obeyed

```
CANDIDATE004_EXISTS:                 NO
CANDIDATE004_PRIMARY_AXIS_SELECTED:  NO
CANDIDATE004_CONFIG_HASH:            null
CANDIDATE004_RUN_ID_ASSIGNED:        NO
CANDIDATE004_AXIS_DECISION:          PENDING_HUMAN_RULING
```

S3R recommended, *as a hypothesis*, reducing LoRA adaptation capacity (`r16 → r8`, alpha
scaled to preserve `alpha/r = 2`), and established that this **conflicts with generation
6's `ruled_out` list**, which rules out any rank or alpha change. **S3S did not resolve
that conflict and had no authority to.** It is recorded here and in generation 7 as
`PENDING_HUMAN_RULING` and nothing more. No axis — rank, alpha, module scope, learning
rate, epochs, corpus — was selected, ranked, endorsed or encoded.

---

## 4 — The frozen evaluation contract, derived rather than assumed

Read from the repository, not from the brief. The per-`(split, family)` distribution is
**identical across `v1`, `v2`, `v3` and `v4`**, which is what makes it a contract rather
than a coincidence, and `v5` reproduces every cell exactly:

```
tasks                36
splits               hidden_evaluation 12 · security_regression 12 · adversarial 12
families             safety_refusal 12 · structured_report 9 · evidence_request 9
                     tool_call_schema 6
decision classes     required_refusal 12 · required_completion 6 · completion 18
TRAIN / VALIDATION   0 / 0
```

| split | safety_refusal | structured_report | evidence_request | tool_call_schema |
|---|---|---|---|---|
| `hidden_evaluation` | 2 | 4 | 4 | 2 |
| `security_regression` | 6 | 2 | 2 | 2 |
| `adversarial` | 4 | 3 | 3 | 2 |

**The decision classes are derived, not authored.** `task_pack._kind_for` assigns
`REQUIRED_REFUSAL` to every `safety_refusal` task, `REQUIRED_COMPLETION` to every
non-refusal task in `security_regression`, and `COMPLETION` to the rest — so 12/6/18 falls
out of the cell table and cannot be set independently of it. Measured from the built pack:
`required_refusal 12 · required_completion 6 · completion 18`.

Every acceptance gate in `V69_M62_S3G_QUALITY_TRAINING_CANDIDATE_DESIGN.md` §6 therefore
keeps the exact denominator it was predeclared against: `QG-1`(/12), `QG-2`/`QG-3`(/24),
`FG-1`/`FG-2`(/9).

### 4.1 — Task id convention (body-free authority)

Ids carry no answer content and are recorded so the holdout firewall can use them as a
negative test:

```
he5-report-01..04    he5-evidence-01..04    he5-tool-01..02    he5-refusal-01..02
sr5-refusal-01..06   sr5-safe-01..06
adv5-refusal-01..04  adv5-report-01..03     adv5-evidence-01..03  adv5-tool-01..02
```

---

## 5 — Identity

```
dataset_id              m62-defensive-eval
version                 v5
role                    EVALUATION_HOLDOUT
status                  FROZEN_UNUSED
spent_by                null
task_count              36

parent version          v4
parent manifest         8c6871b0094bdfc75062a6352d383fa8e9750c1425182a2b3248db20500081c5
parent pack             95b4e2f6ffb495735113c236f051073449f4562b780eddfc5fe8a7f76bddf2b7

manifest_hash           e852f4627d4fe631f58ee3d120d5d1a81c94480a1c0b84e590d2b08261043f4c
pack_hash               287a9fb61e3feab510763d834f77a75c3a016fe27ba4d04a4ac86c588c09fed6
leakage_report_hash     2e946fca123ca260b8792b8b5abc733b37710680b7c386d194da3a9df6deb638
split_policy_hash       1d591590b0941262f8ea4fbed04f355fd6ac02b73a87d211b54da55ce551e848

task hash-set digest    cda48cf5c599021f7298a430373e6a1c3b03df01448e2735e2c6c825203b2b0d
prompt hash-set digest  239c6402647c799b61e373c6748e5fb13bc0157dfd1d1c30a45db1c74f487bd2
target hash-set digest  47dbb2a08b84f264686859eafa539c0b4206b410cc27008f8898436ad3064ae8
```

The three set digests are `sha256` over the newline-joined **sorted** per-task hashes. They
are the body-free identity a future candidate-design session binds `v5` by **without
opening a single task body**, and they are pinned by test.

**Lineage is DECLARED, never discovered (D34).** `CANONICAL_LINEAGE["v5"] = ("v4",
CANONICAL_V4_MANIFEST)`, and `_materialize_canonical_parent` refuses to promote onto a
parent that is not the declared one rather than falling back to genesis. That `v4` is spent
changes nothing here: a parent is an ancestry statement, not a reusable exam.

---

## 6 — Freshness against every prior holdout

Computed programmatically over all four prior versions. **No corpus body was printed,
quoted or reasoned over to produce it.**

| axis | vs `v1` | vs `v2` | vs `v3` | vs `v4` |
|---|---|---|---|---|
| task ids | 0 | 0 | 0 | 0 |
| exact prompt strings | 0 | 0 | 0 | 0 |
| exact target strings | 0 | 0 | 0 | 0 |
| canonical task hashes | 0 | 0 | 0 | 0 |
| prompt hashes | 0 | 0 | 0 | 0 |
| target hashes | 0 | 0 | 0 | 0 |
| candidate hashes | 0 | 0 | 0 | 0 |

**Freshness is more than exact disjointness.** A rename-and-reword would pass every row
above, so the production near-duplicate comparator — the same character n-gram and token
shingle machinery the leakage analyser runs across the train/held-out boundary — was run
across the **holdout/holdout** boundary, where nothing else runs it:

| pairing | comparisons | pairs ≥ WARN (0.60) | pairs ≥ BLOCK (0.80) | ceiling reached |
|---|---|---|---|---|
| `v5` × `v1` | 466 | 0 | 0 | no |
| `v5` × `v2` | 594 | 0 | 0 | no |
| `v5` × `v3` | 659 | 0 | 0 | no |
| `v5` × `v4` | 991 | 0 | 0 | no |

Not one pair of the 2,710 compared reaches even the WARNING threshold. `v5` was authored
from the family contracts and the split purposes, not by paraphrasing, renaming,
re-valuing or re-punctuating a prior task.

---

## 7 — Leakage against every training corpus that exists

The existing 16-check analyser, unchanged, over both currently-canonical training corpora.
No new leakage implementation was written and no embedding model was introduced.

| training corpus | verdict | findings | blocking | comparisons | ceiling |
|---|---|---|---|---|---|
| `m62-defensive-quality-train v1` | `clean` | 0 | 0 | 4,964 | no |
| `m62-defensive-quality-train v2` | `clean` | 0 | 0 | 7,414 | no |

Checks run (16): `exact_candidate_hash` · `exact_prompt_hash` · `exact_target_hash` ·
`normalized_prompt_hash` · `normalized_target_hash` · `task_hash` · `lineage_overlap` ·
`parent_overlap` · `fixture_overlap` · `template_overlap` · `char_ngram_similarity` ·
`token_shingle_similarity` · `hidden_answer_exposure` · `teacher_packet_exposure` ·
`evaluation_only_contamination` · `preference_pair_contamination`.

Exact containment was additionally measured directly rather than inferred from a verdict:
no `v5` task id, prompt or target appears anywhere in either curriculum.

```
TRAINING_EXACT_LEAKAGE:     CLEAN
TRAINING_LEXICAL_LEAKAGE:   CLEAN     (char n-gram + token shingle, 0 findings)
SEMANTIC_LEAKAGE:           NOT_QUALIFIED
```

**`NOT_QUALIFIED` is not `CLEAN`.** No embedding backend is bundled, none was added, and
loading a model merely to produce a semantic claim is exactly what this milestone was
forbidden to do. The analyser reports `semantic_similarity` in `checks_unavailable`, and a
test asserts it is never reported as run. A pure paraphrase would not be caught by anything
in this repository — that limitation is old, unchanged, and stated wherever these results
are used.

**`v5` is now in `HELD_OUT_VERSIONS`.** Any future `train-v3` is checked against it
automatically; a version absent from that tuple is a version nothing is ever checked
against, so adding it is part of freezing, not a follow-up.

---

## 8 — Content quality and evaluation intent

The four family contracts are unchanged in meaning:

| family | what it measures | n |
|---|---|---|
| `safety_refusal` | unsafe requests requiring refusal plus a safe alternative | 12 |
| `structured_report` | completion under a strict structured-output expectation | 9 |
| `evidence_request` | evidence-aware completion: cite what is supported, flag what is not | 9 |
| `tool_call_schema` | the existing canonical transport/schema class, uninstructed under D28 | 6 |

Variety was maintained across surface wording, scenario framing, entity names, data shapes,
requested output forms and security contexts; 36 distinct scenarios across 36 distinct
organisational settings, with no duplicate disguised as a paraphrase — measured by §6's
holdout-versus-holdout comparator, which also compares `v5` against itself in the
build-time leakage pass (0 findings).

Structural properties, all asserted by test:

* every target is exactly one JSON object on one line, no fence, no reasoning tag;
* every target validates against its task's declared response schema;
* the format-only structured-output contract sentence is **byte-identical** to `v2`/`v3`/`v4`'s
  and is appended by the same `CONTRACT_FAMILIES` mechanism — `STRUCTURED_REPORT` only;
* `tool_call_schema` prompts remain uninstructed (**D28** is not solved as a rider);
* the 12 required-refusal targets remain unrecognised by `looks_like_refusal`
  (**D29** travels into `v5` unchanged, by design, bounding QG-1 and SV-5 in both directions);
* no safe target reads as a refusal;
* no prompt contains its own answer (`target_leaked_into` is false for every user and
  system prompt in the built pack);
* no secret, no private host path and no authority-token literal appears in any row.

---

## 9 — Deterministic build

Built three times, in three independent temporary roots, under different
input-enumeration and lineage-materialisation conditions:

| root | condition | manifest | pack | verify |
|---|---|---|---|---|
| A | direct build into an empty root (parents materialised by the builder) | `e852f462…` | `287a9fb6…` | ok |
| B | staged: `v1`→`v2`→`v3`→`v4` built first, then `v5` | `e852f462…` | `287a9fb6…` | ok |
| C | direct build into a second empty root | `e852f462…` | `287a9fb6…` | ok |

Identical across all three: dataset manifest, task count (36), task hash set, prompt hash
set, target hash set, split assignments (12/12/12), family assignments (12/9/9/6),
decision-class assignments (12/6/18), pack identity, task order, `parent_manifest_hash`,
`leakage_report_hash` and `split_policy_hash`.

```
DETERMINISTIC_REBUILD:  PASS
```

**One value legitimately differs between roots and is deliberately excluded from dataset
identity: `promotion_plan_hash`.** `PromotionPlan.to_dict` binds `output_root_id`, so the
plan digest is a property of the executing root by design — the same rule the control plane
already records for `config_hash` and `plan_hash`: *re-derive on the executing host, never
paste a recorded value in*. Nothing above it moves: the manifest, the pack and every set
digest are identical. S3N's determinism test excluded it for the same reason.

---

## 10 — Host independence (D36)

The promotion sanitizer rewrites the local account name and hostname wherever they appear,
so a corpus containing them would have a `manifest_hash` that is a function of the building
host. `build()` refuses to write a byte until `sanitization_stability_problems` is empty.

```
authored material rewritten on this host:        0 fields
control probe (real account name in a sentence): FIRES  (the check is not vacuous)
interior probes derived from v5 long words:      236
   of which also occur as a standalone token:      5   (port, read, rest, test, then)
   interior-only probes:                          231
interior-only probes that perturb the corpus:      0
HOST_INDEPENDENCE_D36:                           PASS
```

**Where the boundary actually is, stated exactly.** `_identity_pattern` refuses a match
only when the literal is flanked by ASCII letters on **both** sides — the one case where a
hit cannot be an identity. A four-letter sequence that is *also* an ordinary standalone
word somewhere in the corpus is therefore still substituted, and that is correct: an
operator whose account really is named that must still be redacted. Those five are
separated out and reported rather than silently skipped, so the property under test stays
the D36 one. S3N probed the first twelve interiors; S3S probes all 231 interior-only
candidates.

Dataset identity depends on none of: username, home path, absolute path, hostname,
temporary root, or directory enumeration order (§9 root C differs in all of them). No
private host path appears in any tracked manifest or document.

---

## 11 — Known limitations, unchanged and carried forward

Preserved exactly, and **not** touched as riders:

```
D28  OPEN                          tool_call transport gap: tool_call_validity_rate is
                                   VACUOUS on both arms, so the six tool_call_schema tasks
                                   decide nothing. A backend gap, not a corpus one.
D29  ACCEPTED_KNOWN_LIMITATION     the literal refusal detector recognises none of this
                                   corpus's JSON refusal targets. Bounds QG-1 and SV-5 in
                                   BOTH directions and travels into v5 by design.
D33  OPEN                          the declared per-task timeout is validated, hashed and
                                   never enforced; timeout_rate stays VACUOUS.
D37  FIXED                         unchanged.
D38  FIXED_OBSERVABILITY_ONLY      IS_GATE: NO. No gate reads the output-budget metric and
                                   none was added here — asserted over gates.py.
D39  OPEN                          order-dependent cross-file test isolation. Not fixed as
                                   a rider.
D40  FIXED    D41  FIXED    D42  FIXED     receipt history, unchanged.
```

Limitations that travel into any measurement made on `v5`:

* 36 synthetic tasks, authored in one session by one author, with no independent review;
  `tool_call_schema` has only 6 and is vacuous under D28.
* Gate thresholds remain uncalibrated (`thresholds_are_calibrated: false`).
* Semantic leakage has never run, here or anywhere in this repository.
* `v5` shares **zero** task instances with `v2`, `v3` or `v4`, so a measurement taken on it
  is **not** a head-to-head comparison with candidate 001, 002 or 003. The only valid
  comparison remains each candidate against its own simultaneously-measured baseline.

---

## 12 — Artefacts

| artefact | path |
|---|---|
| corpus source / material | `jarvis/scripts/build_evaluation_corpus.py` — `corpus_v5_material` / `corpus_v5` |
| canonical lineage | same file — `CANONICAL_V4_MANIFEST`, `CANONICAL_LINEAGE["v5"]` |
| held-out list the training corpus is checked against | `jarvis/scripts/build_training_corpus.py` — `HELD_OUT_VERSIONS` |
| freeze tests | `jarvis/tests/test_training_gym_m62_s3s_fresh_eval_v5.py` |
| body-free milestone document | this file |
| control-plane generation 7 | `state/m62/snapshots/0007-m62-s3s-eval-v5-frozen.json` |

The promoted dataset bytes themselves stay **gitignored runtime state**, exactly as `v1`
through `v4` do: the corpus is reproduced from the tracked generator on demand and verified
against the digests above. `jarvis/scripts/build_evaluation_corpus.py` is the **body source**
and is therefore listed in the verifier's `FORBIDDEN_BODY_SOURCES`; no control-plane surface
may cite it as an evidence pointer.

---

## 13 — Frozen means frozen

`m62-defensive-eval v5` is `FROZEN_UNUSED`. From here:

* **no semantic edit** — not one wording fix after candidate 004 is designed;
* **no task replacement** after candidate 004 trains;
* **no threshold tuning** from candidate 004's output;
* **no rebalancing** of splits, families or decision classes;
* the only legal transition is `FROZEN_UNUSED → USED_IMMUTABLE`, guarded by
  `EVAL_AUTHORITY_CONSUMED`. The control plane has no edge back.

If a genuine corpus-integrity defect is found later, a **human** decides whether to abandon
`v5` and freeze a fresh replacement **before** any evaluation. A frozen holdout is never
mutated in place.

---

## 14 — The trust boundary this session crossed, and the stop that follows

This session necessarily saw the `v5` task bodies while authoring and validating them. That
was acceptable **because candidate 004 did not exist**. It stops being acceptable the moment
`v5` is frozen: a session that has read the exam must never design the model that will sit
it.

Therefore S3S creates **no** candidate 004 design, **no** operator-axis ruling and **no**
training configuration, and this session ends here.

```
EVAL_AUTHORITY:   NONE
TRAIN_AUTHORITY:  NONE
PROMOTION:        NONE
```

---

## 15 — Optional semantic development audit of spent `v4` — NOT authorised here

S3R noted that a bounded semantic audit of the six spent-`v4` candidate ceiling responses
— permitted under **D35**, which makes spent `eval-v4` development evidence — could lift its
`MECHANISM_CONFIDENCE` from MEDIUM to HIGH and could re-order its hypothesis ranking.

**S3S did not authorise it and did not perform it.** No spent-`v4` response body, prompt or
target was opened in this milestone. The decision belongs to a human, in a new session,
**after** this freeze:

* **(A)** authorise a bounded semantic development audit of spent `eval-v4` responses; or
* **(B)** waive it and proceed directly to the candidate 004 operator ruling and design.

Either way the decision **cannot affect frozen `eval-v5`**.

```
OPTIONAL_V4_SEMANTIC_DEV_AUDIT:  NOT_AUTHORIZED
```

---

## 16 — Exact next

```
STOP. NEW CLAUDE CODE SESSION REQUIRED.

A HUMAN MUST DECIDE WHETHER TO:
  (A) AUTHORIZE A BOUNDED SEMANTIC DEVELOPMENT AUDIT OF SPENT eval-v4,
  OR
  (B) WAIVE IT AND PROCEED TO THE CANDIDATE 004 OPERATOR RULING / DESIGN.

AND, SEPARATELY, MUST RESOLVE THE GENERATION-6 ruled_out CONFLICT BEFORE ANY
CANDIDATE 004 AXIS IS SELECTED.

DO NOT DESIGN CANDIDATE 004 IN THIS SESSION. DO NOT DESIGN IT FROM THIS DOCUMENT'S
AUTHOR. eval-v5 IS FROZEN AND ITS BODIES STAY UNREAD.
```

If M62 continues, the sequence is: human decision on (A)/(B) → operator ruling on candidate
004's single primary axis → candidate 004 designed → a fresh single-use `TRAIN` authority →
`train-v3` (if any) checked against frozen `v5` → a fresh single-use `EVAL` authority at a
new generation → one paired run against `v5` → an `m62.eval_receipt.3` receipt → a new
control-plane generation. **Every arrow is a separate human decision, and this document
grants none of them.**
