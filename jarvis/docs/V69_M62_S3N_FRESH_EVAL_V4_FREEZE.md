# V69 M62 S3N — fresh held-out evaluation `v4`: candidate-blind design, qualification and freeze

> **Status: `EVAL_V4_STATUS: FROZEN_UNUSED`.** A fourth held-out eligibility corpus was
> authored candidate-blind, qualified against the frozen evaluation contract, checked for
> freshness and leakage, rebuilt deterministically, and frozen — **before candidate 003
> exists**.
> **Zero training, zero evaluation, zero model generations, zero optimizer steps, no
> `TRAIN` or `EVAL` authority, no candidate 003, no `train-v3`.**

| | |
|---|---|
| Milestone | V69 M62 **S3N** — `m62-defensive-eval v4` only |
| Date | 2026-08-15 |
| Branch | `jarvis-v69-m62-training-gym` |
| Starting HEAD | `4c669fad8a4f576a87b30c919296e316518800fb` |
| Master | `3705114228edef2f665be349c5c4429b7b16777a` (untouched) |
| Preceding milestones | S3M — `V69_M62_S3M_STRUCTURED_OUTPUT_DIAGNOSIS.md` · S3M.1 — `V69_M62_S3M1_D37_TEMPLATE_PARITY_QUALIFICATION.md` · S3M.2 — `V69_M62_S3M2_D38_OUTPUT_BUDGET_INSTRUMENTATION.md` |

---

## 0 — PREREGISTRATION, written before a single `v4` task was authored

**This block was committed to this file before any `v4` material existed.** It exists so
that the session which authors the holdout cannot afterwards shape the model hypothesis
around the test bodies it has just written. It is a record of intent, **not** an
authorisation and **not** a candidate.

```
FUTURE_CANDIDATE003_PRIMARY_AXIS:
    training render reasoning policy   MODEL_DEFAULT -> DISABLED
    (exactly ONE primary model/training axis)

CONTROLLED_LORA_SCOPE:
    ATTENTION_AND_MLP    (q, k, v, o, gate, up, down — candidate 002's seven projections)

CANDIDATE002_TRAINING_CONFIGURATION:
    otherwise FIXED as the future control reference, unless a later milestone discovers a
    genuine blocking incompatibility. r16 / alpha 32 / dropout 0.05, LR 1e-4, 2 epochs,
    max_steps 40, warmup 0.1, batch 1 x grad-accum 8, seed 42, max_sequence_length 512,
    fp32 / CPU, no checkpoints, early stopping disabled, load_best_model_at_end false,
    validation epoch cadence + closing evaluate().

TRAINING_CORPUS_FOR_CANDIDATE_003:
    m62-defensive-quality-train v2  (24ceb1e0…) — unchanged. No train-v3.

EXPLICITLY NOT PART OF CANDIDATE 003:
    ATTENTION_ONLY · learning-rate change · epoch change · rank change · alpha change ·
    dropout change · corpus rebalance · new structured examples · Kimi/Claude-generated
    teacher corpus · max_new_tokens change · gate change · grader change ·
    D38 optimisation.
```

**This preregistration does NOT create candidate 003.** No configuration, no plan, no
adapter identity, no `TRAIN` authority and no `train-v3` exists, and S3N created none.
The D38 output-budget metric is **not** a model axis: it is observational, it applies
symmetrically to both arms, and candidate 003's eligibility remains determined only by the
already-declared security, quality, format and operational gates.

---

## 1 — Authorisation and boundary

The human operator authorised **S3N: fresh held-out evaluation `v4` design, qualification
and freeze** — and nothing else. The scientific rule the milestone exists to enforce is:

> **Freeze the exam before building the student.**

`eval-v3` is `USED_IMMUTABLE`: S3L spent it, and S3M/S3M.2 drew diagnostic evidence from
its body-free results. A third candidate therefore needs a **fourth** holdout, and that
holdout must be frozen while there is still no candidate 003 whose outputs or weights
could have influenced it.

Measured rather than asserted:

```
TRAIN_TOKEN_CREATED / CONSUMED:        NO / NO
EVAL_TOKEN_CREATED / CONSUMED:         NO / NO
MODEL_GENERATIONS:                     0
MODEL_RESPONSE_TOKENS_GENERATED:       0
OPTIMIZER_STEPS:                       0
MODEL_WEIGHTS_LOADED:                  NO   (no tokenizer either)
ADAPTERS_CREATED / MUTATED:            0 / 0
CANDIDATE_003_CREATED:                 NO
CANDIDATE_003_CONFIG_CREATED:          NO
CANDIDATE_003_PLAN_CREATED:            NO
TRAIN_V3_CREATED:                      NO
D37_STATUS:                            FIXED_UNCHANGED
D38_STATUS:                            FIXED_UNCHANGED   (observability only, no gate)
D39_STATUS:                            OPEN_UNCHANGED
GATES / GRADERS / THRESHOLDS:          UNCHANGED
```

**Sealed and not reopened:** the S3I verdict, the S3L verdict, candidate 001, candidate
002, D28, D29, D33, D34, D35, D36, D37, D38 and S3M's diagnosis. Both candidates remain
`EVALUATED_NOT_ELIGIBLE`.

---

## 2 — Why S3N stops before candidate 003

This session **authors** the `v4` task bodies, so it has seen them. A candidate designed in
the same session could be shaped — even unintentionally — around the specific instances it
will later be graded on. Preregistering the model axis (§0) bounds that risk; it does not
eliminate it.

So S3N freezes `v4` and **stops**. A fresh session designs candidate 003 from body-free
`v4` authority only (§10). This is deliberately stricter than "freeze the holdout before
training".

---

---

## 3 — The frozen evaluation contract, derived rather than assumed

Read from the repository, not from the session brief. The per-``(split, family)``
distribution is **identical across `v1`, `v2` and `v3`**, which is what makes it a
contract rather than a coincidence:

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
non-refusal task in `security_regression`, and `COMPLETION` to the rest. So 12/6/18 falls
out of the cell table above and cannot be set independently of it.

`v4` reproduces every cell exactly. Every acceptance gate in
`V69_M62_S3G_QUALITY_TRAINING_CANDIDATE_DESIGN.md` §6 therefore keeps the denominator it
was predeclared against: `QG-1`(/12), `QG-2` and `QG-3`(/24), `FG-1` and `FG-2`(/9).

---

## 4 — Identity

| | |
|---|---|
| Dataset | `m62-defensive-eval` |
| Version | **`v4`** |
| Parent | **`7c948236163198b5de451316e39346a37efcbc1254724f921e116a6c722f75a0`** (= `v3`) |
| Manifest | **`8c6871b0094bdfc75062a6352d383fa8e9750c1425182a2b3248db20500081c5`** |
| Materialized task pack | **`95b4e2f6ffb495735113c236f051073449f4562b780eddfc5fe8a7f76bddf2b7`** |
| Split policy | `e2ff822c0f2de1fe1ed1161174c3abd583ead8d0289dfb92c0daf3d1d2ceb2b3` |
| Promotion plan | `c7ce922debe05794f737020eafdf8b52a88abe79ba8983eaf4ed9202a7c8fd5b` |
| Leakage report (intra-corpus) | `2e946fca123ca260b8792b8b5abc733b37710680b7c386d194da3a9df6deb638` |
| `evaluation_only` / `dataset_eligible` | `true` / `false` |
| Pack blockers | none |
| Status | **`FROZEN_UNUSED`** |

**Do not rediscover the intra-corpus leakage report hash as a collision.** `2e946fca…` is
identical for `v1`, `v2`, `v3` **and** `v4` — measured, in this session. It is a
*structural* digest of a clean 36-record evaluation-only report with the same split counts
and the same checks and **zero findings**; it does not digest task content. The
cross-corpus reports in §7, which do vary with content, are the ones that carry
information.

### 4.1 Body-free set identity — what the next session binds `v4` by

A candidate-design session must be able to name this holdout without reading a task body.
These digests are over the *sorted* canonical hashes, so they identify the set:

```
TASK_HASH_SET_DIGEST    959f28f5b37d1bcc53934a0b5be3055c3b2ce1a4192cd5ae5ec2dc05491f9c68
PROMPT_HASH_SET_DIGEST  26493db629d20973acb6333455d3a3af5f268d98f96de0f2ed2a571cbdbfb11e
TARGET_HASH_SET_DIGEST  916e1ad9a6f41ff3cd4a1719b536036687ce2fc0a94acf2d2900430ecc53c696
```

All three are asserted by
`tests/test_training_gym_m62_s3n_fresh_eval_v4.py::test_the_body_free_set_identities_are_the_frozen_ones`,
so the record above is **enforced**, not filed.

### 4.2 Task ids

Ids carry no answer content, no candidate identity and no host information, and follow the
existing `he…` / `sr…` / `adv…` convention with the version generation in the prefix.

```
hidden_evaluation    he4-report-01..04  he4-evidence-01..04  he4-tool-01..02
                     he4-refusal-01..02
security_regression  sr4-refusal-01..06  sr4-safe-01..06
adversarial          adv4-refusal-01..04  adv4-report-01..03  adv4-evidence-01..03
                     adv4-tool-01..02
```

---

## 5 — Authoring method

The material was authored from the **family contracts and the split purposes**, and from
nothing else.

| family | what the contract asks | what `v4` instantiates |
|---|---|---|
| `structured_report` | one JSON object summarising an incident, severity plus supporting signals, or observation separated from inference | resolver NXDOMAIN burst, cluster role-binding widening, rogue radio broadcast, dead-letter growth at a publisher promotion, attack-surface review, authorised phishing simulation, injection inside an alert body, an unavailable-field demand, severity pressure |
| `evidence_request` | insufficient evidence, conflicting evidence, citation discipline, refusal to derive what the record cannot support | device-code grant location, container-scan records that disagree, audit-entry citation, distinct-user totals from one assertion, secret-scan branch history, firewall-source citation, consensus pressure, injection inside a quoted entry, fabricated-citation pressure |
| `tool_call_schema` | one structured tool call, arguments only | role-binding history, package-release provenance, vulnerability findings, privileged-group membership, sign-in activity under an injected extra argument, baseline drift under an injected destructive call |
| `safety_refusal` | a harmful request that must be refused | cardholder data into a ticket, management-plane exposure, credential-attack tooling, embedded connection strings, human-resources case files, log tampering, API token disclosure, audit suppression, encoding to evade a filter, unbannered social engineering, a capability split across turns, a persona-override attempt |

**Prior holdout bodies were not used as design material.** `v1`/`v2`/`v3` were consulted
through the generator's structure — split/family tuples, task-id convention, the
`CONTRACT_FAMILIES` mechanism, the lineage map — and through the body-free domain summary
in `V69_M62_S3J_SECOND_QUALITY_CANDIDATE_DESIGN.md` §7.3, which exists precisely so a
later author can avoid a domain without reading a task. No `v4` task is a paraphrase, a
noun swap, an address swap or a "harder version" of a prior task. Where an early draft
drifted towards a prior instance — a reused tool name with a different address, and an
inventory-conflict shape `v3` already occupies — the draft was re-authored before the
build, not adjusted afterwards.

**Freshness is then measured, not asserted** (§6, §7).

---

## 6 — Freshness against every prior holdout

Six independent identities, against all three prior versions:

| pairing | task ids | prompts | targets | task hashes | prompt hashes | target hashes | candidate hashes |
|---|---|---|---|---|---|---|---|
| `v4` ↔ `v3` | **0** | **0** | **0** | **0** | **0** | **0** | **0** |
| `v4` ↔ `v2` | **0** | **0** | **0** | **0** | **0** | **0** | **0** |
| `v4` ↔ `v1` | **0** | **0** | **0** | **0** | **0** | **0** | **0** |

All 36 task ids, all 36 prompts, all 36 targets and all 36 canonical task hashes are
distinct within `v4` as well. The canonical task hash is the `TaskSpec` spec hash over the
whole task, so it is the strongest identity the pipeline has.

---

## 7 — Training leakage

The **existing** 16-check analyser, unchanged, run over both corpora in one candidate set
with one split plan. No new leakage implementation, no threshold change and no embedding
dependency was introduced.

| training corpus | candidates compared | verdict | findings | comparisons | ceiling | report hash |
|---|---|---|---|---|---|---|
| `m62-defensive-quality-train v1` (128 rows) | 164 | **CLEAN** | **0** | 4729 | no | `cfe188a9eace258f…` |
| `m62-defensive-quality-train v2` (182 rows) | 218 | **CLEAN** | **0** | 6969 | no | `acab5cc4a6bf43c0…` |

**No train-side split was skipped.** `leakage_against_held_out` builds candidates from the
**whole** curriculum, so the candidate counts above are exactly `128 + 36` and `182 + 36` —
`TRAIN`, `VALIDATION` and both internal held-out splits (`hidden_evaluation`,
`security_regression`) all participate.

Checks run (16, all lexical): `exact_candidate_hash`, `exact_prompt_hash`,
`exact_target_hash`, `normalized_prompt_hash`, `normalized_target_hash`, `task_hash`,
`lineage_overlap`, `parent_overlap`, `fixture_overlap`, `template_overlap`,
`char_ngram_similarity`, `token_shingle_similarity`, `evaluation_only_contamination`,
`hidden_answer_exposure`, `teacher_packet_exposure`, `preference_pair_contamination`.

Exact containment was also measured directly, outside the analyser: **0** of `v4`'s ids,
prompts or targets appears in either training curriculum.

### 7.1 Semantic leakage

```
SEMANTIC_LEAKAGE: NOT_QUALIFIED
```

`semantic_similarity` is reported by the analyser as **unavailable**. No embedding backend
is bundled, and installing one for this milestone was out of scope. **Lexical cleanliness
is not evidence of semantic independence**: a paraphrase sharing no character 5-grams and
no token shingles would not be detected. This is stated as a limitation and pinned by
`test_semantic_leakage_is_reported_unavailable_and_never_clean`, so a future reader cannot
mistake `verdict: clean` for a semantic result.

---

## 8 — Sanitization and D36 host-identity stability

`build()` runs `sanitization_stability_problems` over every authored prompt and target
**before a byte is written**, and refuses the build if the production promotion sanitizer
would rewrite any of them.

```
SANITIZATION_STABILITY_PROBLEMS:   []   (72 fields: 36 prompts + 36 targets)
PRIVATE_PATH_FINDINGS:             0
SECRET_FINDINGS:                   0    (core.redaction_policy.scan_for_leaks, all 72 fields)
AUTHORITY_TOKEN_LITERALS:          0
```

**The control is not vacuous on this host**, and that is asserted rather than assumed: a
synthetic probe carrying this host's account name produces exactly one problem naming D36.

**Host-identity stability was exercised against this corpus, not against a fixture.**
`test_a_host_identity_buried_in_a_v4_word_cannot_perturb_the_corpus` monkeypatches the
local account name to twelve different four-letter interiors of `v4`'s own long words and
asserts the corpus stays byte-stable each time. That is the D36 rule — an identity matches
unless it is flanked by ASCII letters on both sides — applied to the material it must
protect. **The sanitizer itself was not touched**, no string was hand-replaced, and no
promoted byte was patched.

---

## 9 — Deterministic rebuild

Built into **four independent roots** across **two build orders**:

| build | root | order |
|---|---|---|
| A | fresh | direct `v4` (lineage materialises `v1`→`v2`→`v3` first) |
| B | fresh | direct `v4` |
| C | fresh | staged `v1` → `v2` → `v3` → `v4` |
| D | fresh | staged, second independent root |

```
manifest_hash          8c6871b0…   identical in all four
parent_manifest_hash   7c948236…   identical in all four
split_policy_hash      e2ff822c…   identical in all four
leakage_report_hash    2e946fca…   identical in all four
pack_hash              95b4e2f6…   identical across roots
task ordering          identical across roots
verify_version         ok, 0 problems
```

Nothing in the identity depends on a timestamp (`NOW` is the frozen literal
`2026-08-06T00:00:00Z`, asserted by test), a username, a hostname, an absolute path, a
temporary directory or filesystem enumeration order. Pack ordering is the builder's
existing deterministic `(task_hash, task_id)` sort.

---

## 10 — Candidate-blind review

> *"Would this holdout still make sense if candidate 001 and candidate 002 had never
> existed?"*

| family | verdict | why |
|---|---|---|
| `safety_refusal` | **YES** | Twelve instances of the standing safety contract. None exists because candidate 001 over-refused or candidate 002 under-refused; the count is the frozen 12 and the 2/6/4 split across hidden/security/adversarial is the frozen one. |
| `structured_report` | **YES** | Nine instances of "produce the incident object". Deliberately **not** enlarged despite S3M's structured-output diagnosis, and no schema was strengthened, loosened or deepened. |
| `evidence_request` | **YES** | Insufficient evidence, conflicting evidence, citation discipline and refusal to fabricate — the standing evidence contract, unchanged in shape and difficulty. |
| `tool_call_schema` | **YES** | Six structured tool-call proposals, still **uninstructed** under D28. The family was not enlarged, instructed or re-schema'd because candidate 002 emitted zero tool calls. |

No task was written from a candidate's measured failure. The four `structured_report`
failures S3M names (`adv-report-03`, `he-report-04`, `he3-report-01`, `he3-report-04`)
were **not opened** — not their prompts, not their targets — and none of them has a `v4`
counterpart, variant or "harder version".

### 10.1 Difficulty tuning

```
DIFFICULTY_TUNED_AGAINST_A_CANDIDATE_SCORE:  NO
```

Nothing was made easier because both candidates scored 7/9 on structured output, and
nothing was made harder because candidate 002 lost every required refusal. **D29 travels
into `v4` unchanged and deliberately**: the twelve required-refusal targets are
`{"decision": "refuse", …}` objects that `looks_like_refusal` does **not** recognise,
exactly as in `v1`/`v2`/`v3`. Rewriting them so that they *are* recognised would address
D29 as a rider **and** silently change what QG-1 and SV-5 measure between candidate 002 and
candidate 003. A test pins both halves: refusal targets stay unrecognised, and no
required-completion target reads as a refusal.

---

## 11 — Distribution-drift review

Body-free structural comparison of `v4` against `v3`, through the built task pack — no
response behaviour, no score, no candidate output.

| dimension | result |
|---|---|
| task count | SAME (36) |
| decision-class distribution | SAME (12 / 6 / 18) |
| family distribution | SAME (12 / 9 / 9 / 6) |
| split distribution | SAME (12 / 12 / 12) |
| per-`(split, family)` cells | SAME, all twelve |
| grader assignment per family | SAME (`json_schema+secret_pii` 9, `evidence_citation+secret_pii` 9, `safety_policy+secret_pii` 12, `tool_call_schema+secret_pii` 6) |
| mandatory graders | SAME |
| response-schema classes | SAME (30 × `{"type":"object","additionalProperties":true}`, 6 × the tool-call schema) |
| tool-contract classes | SAME (6 tasks carry 6 tool schemas; 30 carry none) |
| `refusal_expected` | SAME (12 true / 24 false) |
| `security_required` | SAME (18 / 18) |
| sensitivity class | SAME (`internal` ×36) |
| system prompts | SAME (all empty) |
| pack eligibility blockers | SAME (none) |

```
EVAL_DISTRIBUTION_DRIFT: NONE
```

---

## 12 — The instrument did not move

Re-derived this session, not pasted:

| identity | value | moved? |
|---|---|---|
| `gate_policy_hash` | `e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5` | **no** |
| `metric_policy_hash` | `e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a` | **no** (S3M.2's value) |
| `generation_policy_hash` | `c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7` | **no** |
| `REASONING_POLICY` | `DISABLED` | no |
| `MAX_NEW_TOKENS` | **512** | no |
| D38 as a gate | **none** — 0 references in `gates.py` | no |

**One reconciliation, recorded so it is not rediscovered as drift.**
`eligibility_generation_policy()` alone hashes to `1b4696d6…`, not `c6b0b682…`, because it
carries library defaults (`timeout_s` 120, `seed` 0, `auto_safe` device and precision).
`c6b0b682…` is the policy the sealed S3I and S3L configuration **documents** declare
(`timeout_s` 300, `seed` 11, `cpu`, `fp32`), and it re-derives byte-identically from both
of them. The digest of record belongs to the configured policy, not to the constructor
default.

```
S3N_CHANGED_ANY_POLICY_GRADER_GATE_OR_METRIC_SOURCE:  NO
```

No file under `training_gym/` was changed. Asserted by a test, over `git diff` against the
starting commit.

---

## 13 — Source change accounting

Production changes are **minimal**, exactly as §46 of the brief requires: the existing
corpus infrastructure supported a fourth version cleanly and no framework change was
needed.

| kind | files |
|---|---|
| **Production (changed)** | `scripts/build_evaluation_corpus.py` — `corpus_v4_material()`, `corpus_v4()`, `CANONICAL_V3_MANIFEST`, the `v4` lineage entry, `CORPUS_VERSIONS`, `LATEST_DATASET_VERSION`, the CLI help string · `scripts/build_training_corpus.py` — **one line**: `HELD_OUT_VERSIONS` gains `"v4"` |
| **Production (new)** | none |
| **Tests (new)** | `tests/test_training_gym_m62_s3n_fresh_eval_v4.py` (60 tests) |
| **Tests (changed)** | two, both "a list moved because a version was added" — §14.1 |
| **Docs** | this file, `PROGRESS.md` |

`HELD_OUT_VERSIONS` is production, not bookkeeping: a held-out version absent from that
tuple is a version the training corpus is **never checked against**. Adding `v4` is part of
freezing it.

---

## 14 — Tests

**New file:** `tests/test_training_gym_m62_s3n_fresh_eval_v4.py` — **60 tests, 60 passed.**

It contains **no `v4` or `v3` prompt, target or task body**, and a test asserts that by
searching its own source for every one of them. A future reader of the suite cannot learn
the holdout from the tests that protect it.

Coverage against the brief's thirty requirements: `v4` exists only as a new version and is
not an alias for `v3`; `v1`/`v2`/`v3` material and manifests unchanged; task count; family
distribution; split distribution; decision classes; id uniqueness; zero prior id, prompt,
target, task-hash, prompt-hash, target-hash and candidate-hash overlap; zero exact train
overlap on both training versions; the frozen lexical leakage policy on both; semantic
leakage reported unavailable and never clean; sanitization stability and its non-vacuity;
host identity cannot perturb the corpus; private-path and secret scans; single-JSON-object
targets; schema validation through real `jsonschema`; the format-only contract sentence on
exactly the contract family; D28 still uninstructed; the D29 refusal-phrasing pin in both
directions; evaluation-only and never dataset-eligible; `TRAIN`/`VALIDATION` refused in
both directions; deterministic rebuild of the manifest and the pack across roots and build
orders; ordering determinism; no timestamp in the identity; prior and training manifests
unchanged; gate, metric and generation policy digests; `max_new_tokens` 512; D38 read by
no gate; no `training_gym/` source changed; no candidate-003 artefact; no `train-v3`.

### 14.1 Two pre-existing tests updated, both deliberately

Both are the shape S3J already recorded — *a list moved because a version was added* — and
neither assertion was weakened:

* `test_v3_declares_an_explicit_deterministic_lineage` (S3J). What S3J owns is **v3's
  parent**, and that assertion is untouched: `v3` still declares `v2` and still hashes to
  `7c948236…`. The version list and `LATEST_DATASET_VERSION` move, and a docstring now says
  why.
* `test_the_generator_names_exactly_the_versions_it_can_build` (S3F.2). Same list, same
  reason. Every other assertion in that file still measures `v2`.

### 14.2 Non-vacuity, demonstrated rather than claimed

In a throwaway worktree at the starting HEAD with the S3N diff applied, five bounded
deliberate mutations. The control run in the same worktree is **59 passed, 1 skipped** (the
skip is the sealed S3L config, a gitignored runtime artefact absent from a fresh worktree).

| mutation | what it breaks | result |
|---|---|---|
| **A** — duplicate a task id | id uniqueness | **3 failed + 11 errors** — the corpus **refuses to promote**, so every test needing a built root errors. Fail-closed, working. |
| **B** — move one task between families | the family/cell contract | **6 failed** — family distribution, the cell-for-cell gate denominators, and all three identities |
| **C** — plant an exact train-v2 prompt on a non-contract task | training leakage | **8 failed** — the leakage analyser fires on **both** training versions, both exact-containment tests fire, and the identities move |
| **D** — insert this host's account name into a prompt | D36 host-identity stability | **3 failed + 11 errors** — the fail-closed stability control refuses the build |
| **E** — change **one** promoted byte in a target | identity | **4 failed** — manifest hash, pack hash and the body-free task/target set digests all move |

Mutation C was re-authored once: a first attempt planted a prompt that the contract
sentence and quote-escaping made non-identical, so no exact overlap existed and the
leakage tests correctly stayed green. That is recorded rather than quietly fixed — a
mutation that does not create the defect it names demonstrates nothing.

The worktree was removed and `git worktree prune` is clean.

### 14.3 Suite results

| scope | result |
|---|---|
| New S3N file alone | **60 passed, 0 failed** |
| Adjacent — S3J · S3I.1 lineage · S3F.2 eval-v2 · evaluation corpus · pack builder · task pack · S3G quality corpus (D36) | **261 passed, 0 failed** (75 s) |
| D37 · D38 · S3M diagnosis files | **180 passed, 0 failed** |
| **Focused M62 (`-k m62`, `--ignore=tests/test_live_brain_v61.py`)** | **3076 passed, 18 skipped, 0 failed** (3m12s) |

**3076 reconciles exactly:** S3M.2's **3015** + the **60** new S3N tests + **1**. The extra
one is not a surprise and not drift: the pre-existing S3G test
`test_the_training_corpus_does_not_leak_the_held_out_corpus` is parametrized over
`QC.HELD_OUT_VERSIONS`, which grew from three entries to four, so it now also runs against
`v4` — and passes. That is the existing leakage authority covering the new holdout, which
is the entire reason that tuple is production.

**The full inner suite was not re-run.** No shared infrastructure changed: the diff is two
corpus generators, one new test file, two list updates and documentation. The authoritative
regression signal for this tree is the focused M62 run above, per existing project policy.

**D39 was not triggered and not fixed.** The authoritative `-k m62` collection is
alphabetical, which is why it is clean; nothing here touches either file involved.
`D39_REPRODUCED: NO`.

### 14.4 Static and security gates

| gate | result |
|---|---|
| `git diff --check` | **PASS** |
| `compileall` over every changed/new file | **PASS** |
| **Ruff** | **NOT RUN — absent from this host**, reported rather than silently skipped |
| **Bandit** | **NOT RUN — absent from this host** |
| Secret scan (`core.redaction_policy.scan_for_leaks`) | **PASS** — findings named below, none suppressed |
| Host-path scan over added lines | **PASS** — the only `/home/` string is inside the assertion that the corpus contains **no** host path |
| `TRAIN:` / `EVAL:` token literal scan | **PASS** — none |
| Runtime artefact exclusion | **PASS** — `git check-ignore` confirms `training_gym_datasets/`, `evaluation/evaluations/` and `training_runs/`; `git ls-files` under all three is empty |
| Body-free audit of the corpus material | **PASS** — 0 secrets, 0 private paths, 0 credentials, 0 real hosts |

The scanner reports the `reasoning` category on three files. On
`scripts/build_training_corpus.py` and `tests/test_training_gym_m62_s3j_second_candidate.py`
it is **byte-identically present at the starting HEAD** — verified by scanning the HEAD
blobs — so S3N added neither. On the new S3N test file it is a single line: the assertion
`assert "<think" not in target`, i.e. the literal inside the check that the corpus contains
none. Operator ruling **H4** classifies reasoning markup as hygiene rather than a security
leak, exactly as S3G, S3J, S3M, S3M.1 and S3M.2 recorded.

---

## 15 — Pre-freeze acceptance criteria

| | criterion | result |
|---|---|---|
| **H1** | Git authority clean | **PASS** — HEAD `4c669fa`, `0 0`, master unchanged, tree clean |
| **H2** | D37 and D38 re-derived as frozen | **PASS** — §12 |
| **H3** | evaluation contract identified without ambiguity | **PASS** — §3, identical across v1/v2/v3 |
| **H4** | task count matches the contract | **PASS** — 36 |
| **H5** | family counts match | **PASS** — 12/9/9/6 |
| **H6** | split counts match | **PASS** — 12/12/12 |
| **H7** | every task id unique | **PASS** |
| **H8** | no task-id overlap with v1/v2/v3 | **PASS** — 0 |
| **H9** | no canonical task-hash overlap | **PASS** — 0 |
| **H10** | no prompt-hash overlap | **PASS** — 0 |
| **H11** | no target-hash overlap | **PASS** — 0 |
| **H12** | no exact train-side prompt/target overlap | **PASS** — 0, both training versions |
| **H13** | existing lexical leakage policy passes | **PASS** — CLEAN, 0 findings, thresholds untouched |
| **H14** | semantic leakage reported honestly | **PASS** — `NOT_QUALIFIED` |
| **H15** | sanitization stability passes | **PASS** — 0 problems, control non-vacuous |
| **H16** | no private host identity leaks | **PASS** |
| **H17** | no secret/token leaks | **PASS** |
| **H18** | schema validation passes | **PASS** — real `jsonschema`, all 36 |
| **H19** | task-family contract validation passes | **PASS** — cells, decision classes, contract sentence, D28, D29 |
| **H20** | deterministic rebuild passes | **PASS** — 4 roots, 2 build orders |
| **H21** | manifest rebuild byte/hash stable | **PASS** |
| **H22** | old eval manifests unchanged | **PASS** — `0970600c…`, `82b60bfd…`, `7c948236…` |
| **H23** | train-v1/train-v2 unchanged | **PASS** — `9bbac2f0…`, `24ceb1e0…` |
| **H24** | no training/eval authority exists | **PASS** — none created |
| **H25** | no candidate-003 artefact/config/plan | **PASS** — none, asserted over tracked files |
| **H26** | no model generation occurred | **PASS** — 0 |
| **H27** | no policy/gate/metric source changed | **PASS** — no `training_gym/` file changed |
| **H28** | no D39 rider fix | **PASS** — untouched |

**All twenty-eight passed, so `v4` was frozen.**

---

## 16 — Freeze declaration

```
EVAL_V4_STATUS: FROZEN_UNUSED
```

`m62-defensive-eval v4` is **immutable** from this point. It has never been read by a
model, no inference has been run against it, and no `EVAL` authority binds it.

**If a defect is found in `v4` after this freeze, it is NOT edited in place.** A corrected
holdout is a **new dataset version** with a new identity, a declared lineage onto `v4`, and
its own freshness and leakage qualification — the same rule that produced `v2`, `v3` and
`v4` themselves.

The freeze binds: the authored source (`corpus_v4_material`), the promoted task identities,
the manifest hash, the pack hash, the parent, the task count, and the family/split/decision
structure.

---

## 17 — The holdout body firewall

`v4`'s task bodies are **sealed from candidate-design work from this point.** S3N may read
them because S3N authored them. **No candidate-003 design session may.**

**A future candidate-003 design session MAY use:**

* the dataset id `m62-defensive-eval` and the version `v4`;
* the manifest hash `8c6871b0…` and the parent `7c948236…`;
* the pack hash `95b4e2f6…`;
* the task count, family counts, split counts and decision classes;
* the task ids (§4.2) and the body-free set digests (§4.1);
* the leakage statuses and the frozen policy identities (§12).

**It may NOT use:** any `v4` prompt, any `v4` target, any hidden target, any task body, or
any task-specific semantic content.

**Candidate 003 training may NEVER use `v4`.** Candidate-003 debugging may never turn a
`v4` failure into a training example. Once candidate 003 is evaluated against it, `v4`
becomes `USED_IMMUTABLE` and a fourth candidate needs a fifth holdout — the same D35 rule
that produced this one.

---

## 18 — Limitations

1. **Semantic independence is not established.** `SEMANTIC_LEAKAGE: NOT_QUALIFIED`. Every
   freshness result here is exact or lexical.
2. **The corpus is synthetic, 36 tasks, authored in one session by one author, with no
   independent review** — PROGRESS §14.3, unchanged. `tool_call_schema` still has only 6
   tasks, and `min_pairs_for_claim` is still cleared rather than comfortably exceeded.
3. **No model has seen `v4`.** What it will measure is **unknown, not estimated**. Nothing
   here predicts that candidate 003 will score better, or differently, than 001 or 002.
4. **D28 is not solved.** The tool-call family remains uninstructed and its metric will be
   vacuous again unless the backend gains a transport for `proposed_tool_calls`.
5. **D29 is not solved**, and travels into `v4` deliberately (§10.1). It will bound QG-1
   and SV-5 in both directions again.
6. **D33 is not solved.** A declared timeout is still not enforced.
7. **D39 is open**, untouched.
8. **`v4` is comparable to nothing yet.** Candidate 001 was measured on `v2` and candidate
   002 on `v3`, with zero shared task instances. What is comparable is each candidate
   against its **own** simultaneously-measured baseline under identical policy digests —
   which is what every gate already does. `v4` does not change that and does not make the
   three candidates rankable against each other.
9. **A candidate fitted under `DISABLED` is not directly comparable to 001 or 002**
   (PROGRESS §14.84). Freezing `v4` does not soften that; it is a property of the
   preregistered axis, not of the exam.

---

## 19 — What future sessions must NOT do

- **DO NOT** modify `m62-defensive-eval v4` — not a prompt, not a target, not an id, not a
  count. If it must change, that is a **new version**, not an edit.
- **DO NOT** read `v4` task bodies while designing, configuring, planning, training or
  debugging candidate 003. §17 lists exactly what may be used instead.
- **DO NOT** train on `v4`, derive training examples from it, or turn a `v4` failure into
  data augmentation.
- **DO NOT** reuse `eval-v3` as a fresh holdout. It is `USED_IMMUTABLE`.
- **DO NOT** create candidate 003 in the session that authored `v4`. That session has seen
  the bodies; the firewall is the point.
- **DO NOT** change the preregistered candidate-003 primary axis
  (`MODEL_DEFAULT` → `DISABLED`), and **do not** combine it with `ATTENTION_ONLY` — that is
  two variables and a third uninterpretable run (PROGRESS §14.84).
- **DO NOT** create `train-v3`, add training rows, rebalance `train-v2`, or introduce a
  Claude/Kimi-generated teacher corpus. Candidate 003's corpus is `train-v2`, unchanged.
- **DO NOT** modify D37 or D38, and **do not** turn D38 into a gate.
- **DO NOT** raise `max_new_tokens`, strengthen the `structured_report` schema, or widen
  `looks_like_refusal` on the strength of anything in this document.
- **DO NOT** re-derive the S3N freshness, leakage or rebuild evidence to confirm it. Four
  roots, two build orders, 0 findings in every pairing, 0 overlap on six identities.
- **DO NOT** rediscover the shared intra-corpus `leakage_report_hash 2e946fca…` as a
  collision (§4).
- **DO NOT** fix **D39** as a rider.

---

## 20 — Final status

```
S3N_FRESH_EVAL_V4_FREEZE:              PASS
STARTING_HEAD:                         4c669fad8a4f576a87b30c919296e316518800fb

EVAL_V4_DATASET_ID:                    m62-defensive-eval
EVAL_V4_VERSION:                       v4
EVAL_V4_STATUS:                        FROZEN_UNUSED
EVAL_V4_TASK_COUNT:                    36
EVAL_V4_FAMILY_COUNTS:                 safety_refusal 12 · structured_report 9 ·
                                       evidence_request 9 · tool_call_schema 6
EVAL_V4_SPLIT_COUNTS:                  12 / 12 / 12
EVAL_V4_DECISION_CLASSES:              12 required_refusal · 6 required_completion ·
                                       18 completion
EVAL_V4_MANIFEST_HASH:                 8c6871b0094bdfc75062a6352d383fa8e9750c1425182a2b3248db20500081c5
EVAL_V4_PARENT:                        7c948236163198b5de451316e39346a37efcbc1254724f921e116a6c722f75a0
EVAL_V4_PACK_HASH:                     95b4e2f6ffb495735113c236f051073449f4562b780eddfc5fe8a7f76bddf2b7

PRIOR_TASK_ID_OVERLAP:                 0      PRIOR_TASK_HASH_OVERLAP:   0
PRIOR_PROMPT_HASH_OVERLAP:             0      PRIOR_TARGET_HASH_OVERLAP: 0
TRAIN_EXACT_LEAKAGE:                   CLEAN  TRAIN_LEXICAL_LEAKAGE:     CLEAN (0 findings)
SEMANTIC_LEAKAGE:                      NOT_QUALIFIED
SANITIZATION_STABILITY:                PASS   HOST_IDENTITY_STABILITY:   PASS
DETERMINISTIC_REBUILD:                 PASS   REBUILD_MANIFEST_MATCH:    YES
OLD_EVAL_MANIFESTS_UNCHANGED:          YES    TRAIN_MANIFESTS_UNCHANGED: YES
EVAL_DISTRIBUTION_DRIFT:               NONE   CANDIDATE_BLIND_REVIEW:    YES (all 4 families)

D37_STATUS:  FIXED_UNCHANGED    D38_STATUS: FIXED_UNCHANGED    D38_IS_GATE: NO
D39_STATUS:  OPEN_UNCHANGED
GATE_POLICY_HASH:        e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5
METRIC_POLICY_HASH:      e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a
GENERATION_POLICY_HASH:  c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7
REASONING_POLICY:        DISABLED           MAX_NEW_TOKENS: 512

FUTURE_CANDIDATE003_PRIMARY_AXIS_PREREGISTERED:  MODEL_DEFAULT_TO_DISABLED
FUTURE_CANDIDATE003_LORA_SCOPE:                  ATTENTION_AND_MLP
CANDIDATE_003_CREATED / CONFIG / PLAN:           NO / NO / NO
TRAIN_V3_CREATED:                                NO
EVAL_V3:                                         USED_IMMUTABLE
TRAIN_TOKEN_CREATED / CONSUMED:                  NO / NO
EVAL_TOKEN_CREATED / CONSUMED:                   NO / NO
MODEL_GENERATIONS:                               0
MODEL_RESPONSE_TOKENS_GENERATED:                 0
MODEL_WEIGHTS_LOADED:                            NO
OPTIMIZER_STEPS:                                 0
HOLDOUT_BODIES_ALLOWED_IN_NEXT_CANDIDATE_DESIGN: NO
MODEL_PROMOTION:  NOT_AUTHORIZED    MODEL_REGISTRY_MUTATED: NO
MERGE / TAG / RELEASE / VERSION_BUMP:  NO / NO / NO / NO
```

---

## 21 — Exact NEXT

**S3N is closed and authorises nothing further.** It froze an exam. It designed no student.

The next step is a **NEW Claude session** performing **candidate-003 controlled design**,
and it is separated from this one on purpose: this session has seen `v4`'s task bodies, and
the next one must not.

That session may read:

* `PROGRESS.md`;
* this document — **excluding nothing, because this document contains no task body**;
* `V69_M62_S3M1_D37_TEMPLATE_PARITY_QUALIFICATION.md` and
  `V69_M62_S3M2_D38_OUTPUT_BUDGET_INSTRUMENTATION.md` as frozen instrument authority;
* candidate 002's training authority (`V69_M62_S3J_SECOND_QUALITY_CANDIDATE_DESIGN.md`,
  `V69_M62_S3J1_KALI_TRAINING_RUNTIME_QUALIFICATION.md`, `V69_M62_S3K_…`);
* the body-free `v4` authority in §4, §4.1, §4.2 and §17.

It may **not** read `v4` task bodies — not `corpus_v4_material()`, not the promoted
shards, not `task-pack.jsonl`.

It must change **exactly one** primary model/training axis
(`MODEL_DEFAULT` → `DISABLED`), keep LoRA scope `ATTENTION_AND_MLP`, keep candidate 002's
measured configuration otherwise fixed, train on `train-v2` unchanged, and create no
authority: live training needs a separate `TRAIN` authorisation, and evaluation needs a
separate single-use `EVAL` authority at a new generation.
