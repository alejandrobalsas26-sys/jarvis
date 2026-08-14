# V69 M62 S3J — Second quality-candidate design

> **Status:** design / dataset / holdout / plan-qualification milestone. **No training ran,
> no evaluation ran, no `TRAIN` or `EVAL` authority was created, and zero model response
> tokens were generated.**
>
> **The first quality candidate is unchanged.** `qwen3-06b-lora-quality-live-001` remains
> `EVALUATED_NOT_ELIGIBLE`, its adapter `43213035…` is untouched, and the S3I LIVE
> evaluation of record is not re-run, re-scored, replayed or reinterpreted anywhere in
> this milestone.

| | |
|---|---|
| Date | 2026-08-14 |
| Branch | `jarvis-v69-m62-training-gym` |
| Host | Kali Linux, the clean clone, the gitignored Linux runtime qualified in S3I.1 |
| Preceding milestone | **S3I LIVE** — `jarvis/docs/V69_M62_S3I_LIVE_QUALITY_HELDOUT_EVALUATION.md` |
| Second candidate | `qwen3-06b-lora-quality-live-002` — **`DESIGNED_UNTRAINED`** |

---

## 1 — Operator authorisation

The human operator explicitly accepted the sealed S3I result and authorised S3J as a
design / dataset / holdout / plan-qualification milestone. It was authorised to read
authoritative state, analyse the already-sealed body-free S3I metrics, design the second
candidate, create a new training-corpus version and new independently authored examples,
create and freeze a new eligibility corpus **before** training, run leakage checks and
tests, predeclare the acceptance contract, design the second LoRA configuration, produce
a deterministic training-plan **preview**, qualify tokenizer lengths, update
documentation and PROGRESS, commit and push.

It was **not** authorised to train, to create or consume a `TRAIN` or `EVAL` token, to
change model weights, to create an adapter, to run held-out inference, to replay or
rescore S3I, to inspect S3I raw response bodies, to train on eval-v2 task text, to
promote, activate, mutate the Model Registry, merge, tag, release or bump a version.

**Every one of those is `NO` in §16.**

---

## 2 — What S3I measured, and what S3J is allowed to know about it

The S3I LIVE evaluation ran 36 + 36 = 72 real held-out generations against
`m62-defensive-eval v2` and returned a decision against gates fixed **before any
training existed**. `S3I_LIVE_EVALUATION: PASS` and `CANDIDATE_ELIGIBILITY: NOT_ELIGIBLE`
are both true and are not in tension: the instrument worked and the candidate did not
clear the bar.

### 2.1 The gain that must be preserved

| Measure | Baseline | Candidate |
|---|---|---|
| Security vetoes SV-1…SV-9 | — | **all PASS** |
| Required-refusal success | 1/12 | **9/12** (sign test p = 0.0039) |
| Critical safety violations | 11 | **3** |
| Secret / PII findings | 1 | **0** |
| New security findings | — | **0** |
| Security regressions | — | **0** |
| Task success | 25/36 | **31/36** |
| Normalized reward | 0.569319 | **0.727133** |
| Aggregate paired sample (QG-4) | 30 tasks | 16 W / 8 T / 6 L, W−L **+10**, CI95 **[+0.0882, +0.4203]** |

### 2.2 The defects that must be repaired

| Gate | Measure | Baseline | Candidate | Verdict |
|---|---|---|---|---|
| QG-2 | unnecessary refusal | 0/24 | **2/24** | FAIL (absolute, no-new-over-refusal) |
| QG-3 | safe completion | 24/24 | **22/24** | FAIL (baseline-relative) |
| FG-1 | JSON parseability | 9/9 | **7/9** | FAIL (`>=` baseline) |
| FG-2 | schema validity | 9/9 | **7/9** | FAIL (`>` baseline) |

`FG-3` (reasoning hygiene) and `FG-4` (evidence validity) PASS, as do `QG-1`, `QG-4` and
`OG-1…OG-7`.

### 2.3 The anti-overfitting rule, and how it was enforced

S3J exists **because** those metrics are now known. That makes the holdout a development
input and creates an obvious way to cheat. The design input was therefore restricted to
two **aggregate** facts and nothing finer:

* *two safe-task over-refusals occurred*;
* *structured-output performance fell from 9/9 to 7/9*.

Concretely, and checked rather than promised:

* **No held-out prompt, target, entity, value or task id** was copied, paraphrased or
  templated into training material. The two failing task ids named by S3I are diagnostic
  evidence only; no training row derives from them and no test requires candidate 002 to
  answer them.
* **Exact overlap is zero in every direction** — ids, prompts, targets and task hashes —
  between `m62-defensive-quality-train v1`/`v2` and `m62-defensive-eval v1`/`v2`/`v3`
  (§7.3).
* The existing lexical leakage analyser was run over all six pairings and returns
  **CLEAN, 0 findings** (§7.4).

**One disclosure, so it is on the record rather than inferred.** `m62-defensive-eval v3`
had to be authored inside `scripts/build_evaluation_corpus.py`, which is the same file
that holds the `v1`/`v2` material. That file was therefore read in full, and the two task
ids S3I named were visible in it. What was taken from it is the *structure* the gates
read — three splits at 12/12/12, four families at 12/9/9/6, decision classes 12/6/18, and
the format-only output-contract mechanism — and nothing else. The overlap and leakage
measurements above are what make that claim falsifiable.

---

## 3 — D35: eval-v2 becomes development evidence

```
D35_OPERATOR_DECISION:  EVAL_V2_BECOMES_DEVELOPMENT_EVIDENCE_FOR_S3J
```

`m62-defensive-eval v2` remains **immutable**, remains **authoritative for S3I**, and
remains the **run-of-record corpus for the first candidate**. Nothing about it is
contaminated and nothing about it is being rewritten.

What changed is its *role*. Its measured per-gate results are now shaping the second
candidate's curriculum, and a holdout whose failures informed the next model's training
is no longer a held-out measurement of that model — it is development evidence. This is a
**model-selection methodology** ruling, not a content one.

Consequence: **S3J freezes a new evaluation corpus, `m62-defensive-eval v3`, before
candidate 002 is trained.** `v1` and `v2` remain historical and buildable.

The same rule applies forward: a *third* candidate informed by v3's results would require
another fresh holdout.

---

## 4 — Candidate 001 is immutable

```
FIRST_CANDIDATE:          qwen3-06b-lora-quality-live-001
STATUS:                   EVALUATED_NOT_ELIGIBLE
ADAPTER_SHA256:           43213035c15cd38928d2d6a3bdbd9af96872a954801c6bfd0a9b82a8e22ac858
TRAINING_DATASET:         m62-defensive-quality-train v1 (9bbac2f0…) — UNCHANGED
CONFIG_NOTES_FIELD:       byte-identical (it is inside config_hash)
```

`scripts/build_quality_training_config.py` now configures two candidates. Candidate 001's
`run_id`, `experiment_name`, corpus version, option `B` and — critically — its `notes`
string are reproduced **byte for byte**, because `notes` is one of the fields
`TrainingConfig.config_hash()` covers. Rewording it to read more naturally alongside the
second candidate would silently re-identify the configuration S3H actually trained under.
Verified by rebuilding the configuration under the pre-S3J code and the current code in
the same root: **identical `config_hash`, identical `to_dict()`**.

---

## 5 — D36: a dataset identity may not depend on the host that built it

**Found while reproducing `m62-defensive-quality-train v1` on this host. Fixed here, with
regression tests in both directions.**

### 5.1 The defect

Every promoted prompt and every promoted target passes through
`datasets.promotion.prepare_target_text`, which calls
`teachers.sanitization.sanitize_text`. That function substitutes the local account name
and hostname wherever they appear — matched as a plain, case-insensitive **substring**.

On this host the account name is a four-letter sequence that also occurs inside an
ordinary English word used by one `v1` row. The promoted bytes therefore differed from
the authored bytes, and so did the record digest, the shard digest and the dataset
`manifest_hash`:

```
m62-defensive-quality-train v1, rebuilt on this host, BEFORE the fix
  measured   2ef40bda3e53f2a1164f4d04e4d5ec856ce39c1a2af0bfb612340ad2adaeb79e
  recorded   9bbac2f057fd0592a30a7fdeb968655f8ea585df00966e1b920415377ab7286a
  difference exactly one row; one word rewritten in its prompt and in its target
```

**The promoted `v1` on disk was never affected** — it verifies to `9bbac2f0…` throughout,
and S3H trained on those exact bytes. What was broken was *reproducibility*: the corpus of
record could not be rebuilt on the only host that can currently train or evaluate.

This is the **D34 failure class arriving through a different door** — a dataset identity
that is a function of incidental environment state rather than of the dataset — and the
operator's D34 ruling was explicit that it must not recur.

### 5.2 The fix

`sanitize_text` and `scan_export_payload` now share one definition of an identity match
(`_identity_pattern`): the literal is redacted **unless it is flanked by ASCII letters on
both sides**, which is the one case where the hit cannot be an identity because it is the
middle of a longer word.

Deliberately **narrower than a word boundary**. `\b` would stop matching `name123` and
`name_2`, and those are exactly the shapes a real leak takes. Start or end of string,
punctuation, a digit, a hyphen, a path separator — all still match, all still redacted.

The redactor and the independent verifier use the same helper, so they cannot disagree; a
verifier stricter than the redactor would refuse every payload containing an ordinary word
that happens to spell the account name.

### 5.3 The proof

```
m62-defensive-quality-train v1, rebuilt on this host, AFTER the fix
  9bbac2f057fd0592a30a7fdeb968655f8ea585df00966e1b920415377ab7286a   ✔ matches the record
m62-defensive-eval v1, rebuilt as the control
  0970600c677c89112db972c6024634aa871be92dee303db7f429c90967d3dd3b   ✔ unmoved
m62-defensive-eval v2, rebuilt
  82b60bfdbea263eef3990eb6e49c2f2ca16e9b9e26ec8ac435f314b374279d60   ✔ unmoved
```

No promoted artefact's identity moved. The fix strictly restores reproducibility toward
the recorded history; it does not create a new one.

### 5.4 The fail-closed control

`build_evaluation_corpus.sanitization_stability_problems()` compares every authored prompt
and target against the production sanitizer and refuses the build if any of them would be
rewritten. It runs in **both** generators before a single byte is written, and it is
**not** a second opinion about what "private" means — it calls `prepare_target_text` and
compares.

A future author on a host whose account name spells something ordinary now gets a loud
refusal instead of a silently forked dataset identity.

### 5.5 Consequence for v2

None. With D36 fixed, no `v1` row needs correcting, so **`v2` is `v1` plus additions and
nothing else**. `V2_DATA_INTEGRITY_CORRECTIONS` is present, empty, and asserted empty by a
test, so that property stays checkable rather than asserted.

---

## 6 — `m62-defensive-quality-train v2`

### 6.1 Identity and lineage

| | |
|---|---|
| Dataset | `m62-defensive-quality-train` |
| Version | **`v2`** |
| Parent | **`9bbac2f057fd0592a30a7fdeb968655f8ea585df00966e1b920415377ab7286a`** (= `v1`) |
| Manifest | **`24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f`** |
| Split-plan hash | `17bdd8609b62ba5388810a5ef7fd23926425ec6810d61edafd6736195558dd49` |
| Split-policy hash | `1c8b242a379dbe43cf58877f7d65e2bae77b797170c5655b5787caabf97df842` (identical to `v1`) |
| Leakage report hash | `d2fbea6d…` (see §7.4 for the cross-corpus reports) |
| TRAIN export hash | `82780fa0edc4c99198d0074a8a01b08507fa3eed54b4af50c3e045d5e07ae921` |
| TRAIN export file sha256 | `72065595893decf87b6276595634f01c8dbb2313cbfbbd482bbe660e63166410` |
| VALIDATION export hash | `ac065112c4cb3a2195100c3f11289d1e109f40441d293ded280d9b6cddd540fd` |
| VALIDATION export file sha256 | `7ee612efa0d0609d33fa06bee3057128b3ac0e90cdc54a23d4a5da6d15081c33` |

**Lineage is declared, never discovered** — the D34 rule, applied on the training side.
`CANONICAL_LINEAGE` names `v2`'s parent as a constant; `declared_parent()` materialises the
parent if the destination root lacks it and then **refuses** if it does not verify to the
declared digest. A build that cannot establish its lineage fails closed; it never degrades
to `genesis` and never adopts whichever version it happens to find.

**Determinism.** Built into three roots and two build orders (`v2` direct with the parent
auto-materialised; `v1` then `v2` explicitly): identical `manifest_hash`,
`parent_manifest_hash`, `split_plan_hash`, both export hashes and both export file digests.
`promotion_plan_hash` differs by root, as it must — it binds `output_root_id`.

### 6.2 Distribution

| | v1 | v2 |
|---|---|---|
| Total promoted rows | 128 | **182** |
| TRAIN | 107 | **154** |
| VALIDATION | 9 | **12** |
| Internal `hidden_evaluation` | 6 | **8** |
| Internal `security_regression` | 6 | **8** |
| Train-side rows | 116 | 166 |
| Refusal rows | 37 | **37** (unchanged) |
| Completion rows | 91 | 145 |
| Refusal share | 28.91 % | **20.33 %** |
| Family `SAFETY_REFUSAL` | 79 | 105 |
| Family `STRUCTURED_REPORT` | 21 | 49 |
| Family `EVIDENCE_REQUEST` | 28 | 28 |
| Family `TOOL_CALL_SCHEMA` | 0 | **0** (D28) |

### 6.3 Curriculum taxonomy

| Category | v1 | v2 | Δ |
|---|---|---|---|
| `refusal_direct` | 13 | 13 | — |
| `refusal_redirect` | 13 | 13 | — |
| `adversarial_refusal` | 11 | 11 | — |
| `privacy_discipline` | 4 | 4 | — |
| `evidence_missing` | 12 | 12 | — |
| `evidence_sufficient` | 12 | 12 | — |
| `calibrated_uncertainty` | 4 | 4 | — |
| `structured_soc` | 12 | 12 | — |
| `structured_dfir` | 9 | 9 | — |
| `over_refusal_counterexample` | 17 | 35 | **+18** |
| `safe_completion` | 21 | 29 | **+8** |
| `structured_identity` | 0 | 3 | **+3** |
| `structured_network` | 0 | 3 | **+3** |
| `structured_cloud` | 0 | 3 | **+3** |
| `structured_endpoint` | 0 | 3 | **+3** |
| `structured_evidence_review` | 0 | 3 | **+3** |
| `structured_incident_triage` | 0 | 3 | **+3** |
| `structured_safe_intersection` | 0 | 10 | **+10** |
| **Total** | **128** | **182** | **+54** |

Aggregates the acceptance argument rests on:

```
NEW_ROWS                                54   (48-72 preferred range)
NEW_SAFE_COMPLETION / OVER_REFUSAL      36   (>= 24 required; 34 of them train-side)
NEW_STRUCTURED_OUTPUT                   28   (>= 24 required; 26 of them train-side)
INTERSECTION (both at once)             10   (>= 8 required; 8 train-side)
REFUSAL CURRICULUM                      37 -> 37, every category byte-identical
```

`structured_safe_intersection` is deliberately a member of **both**
`SAFE_COMPLETION_CATEGORIES` and `JSON_ONLY_CATEGORIES`, so those 10 rows count toward both
corrections by definition rather than by double-counting.

### 6.4 Why this shape

**The over-refusal correction is a counterweight, not a deletion.** All 37 refusal rows
survive with every category count unchanged — direct, redirect, adversarial, privacy,
evidence limitation. What moves is the *share*: 28.9 % → 20.3 %, because the completion
side grew by 54 rows. That is the intended direction. Curing over-refusal by thinning the
refusal curriculum would trade away the one thing S3I proved this corpus can do.

**The over-refusal rows are hard negatives on purpose.** They carry exactly the vocabulary
that makes a request *look* unsafe — malware, exploit, shell, credential, payload,
persistence, lateral movement, phishing, command execution, registry, packet capture,
forensic artefacts, vulnerability, IOC, authentication, firewall, IDS/IPS, SIEM, incident
response, Linux permissions — while the task itself is plainly defensive: analyse a
sanitised command string, explain a log entry, write a detection rule from supplied
evidence, classify an IOC, describe remediation, summarise a mock incident, read a
defensive scanner result, or interpret a harmless demonstration snippet the user already
supplied.

**One vocabulary note, so it is not rediscovered as an omission.** The literal token
`PowerShell` (and `nmap`, `curl`, `reg add`, `net user`, `/bin/sh`, …) is refused by the
pre-existing `_COMMANDISH_RE` invariant, which S3J deliberately does **not** weaken — a
corpus that teaches a model to emit literal command lines is a different and riskier
corpus. Those concepts therefore appear by name ("the script host", "an encoded argument",
"script-block logging", "the capture file") and never as an executable token. Twenty of the
twenty-one vocabulary terms the brief listed appear literally; that one appears by concept,
by design.

**The structured correction is six domains, not one schema repeated.** Identity, network,
cloud, endpoint, evidence review and incident triage each use a different shape:
required and optional fields, an omitted-rather-than-null optional, an enum-valued field, a
nested array of typed objects, a nested object plus a boolean, and — deliberately — one row
whose evidence array is legitimately `[]`, so the model learns to represent nothing as
nothing instead of inventing a plausible entry to fill the schema.

**No `tool_call_schema` rows.** D28 remains `NOT_QUALIFIED`: the production backend has no
tool-call transport, so training a capability the instrument cannot observe would buy an
unfalsifiable claim. A test pins their absence.

### 6.5 The split seed is a stability anchor

`SPLIT_SEED` is fixed for **every** version of this dataset rather than derived from the
version. `_assign_group` hashes `(algorithm, seed, group)` per group, so holding the seed
fixed makes `v2` strictly additive:

```
v1 TRAIN (107)      is a strict subset of  v2 TRAIN (154)
v1 VALIDATION (9)   is a strict subset of  v2 VALIDATION (12)
v1 rows that changed side:  0
```

Re-seeding per version would reshuffle 128 already-qualified rows for no reason and make
the two candidates' corpora incomparable.

### 6.6 Validation

12 rows, and it sees every behaviour the run needs to watch:

| Row | Category | Family |
|---|---|---|
| `q-rr-01`, `q-rr-02`, `q-rr-04`, `q-rr-07` | `refusal_redirect` | SAFETY_REFUSAL |
| `q-oc-12`, `q2-oc-15` | `over_refusal_counterexample` | SAFETY_REFUSAL |
| `q-sc-18` | `safe_completion` | SAFETY_REFUSAL |
| `q-sd-03`, `q2-se-01` | structured (DFIR, endpoint) | STRUCTURED_REPORT |
| `q2-x-01` | `structured_safe_intersection` | STRUCTURED_REPORT |
| `q-em-05`, `q-es-04` | `evidence_missing` / `evidence_sufficient` | EVIDENCE_REQUEST |

Zero TRAIN/VALIDATION overlap in both directions; no internal held-out row reaches either
train-side split; **no eval-v3 task can ever enter validation** — `TRAIN_SIDE_SPLITS`
refusal is enforced by three independent authorities and pinned by a test.

Validation loss stays **diagnostic**. It is not an eligibility gate, it appears in no S3G
§6 gate, and it authorises nothing.

### 6.6.1 Machine-audited data contracts

| Check | Result |
|---|---|
| Structured targets: exactly one JSON object, parses | **49 / 49** |
| Structured targets: schema-valid (`jsonschema`, the S2b loader) | **49 / 49** |
| Structured targets: no markdown fence, no `<think>`, no surrounding prose | **49 / 49** |
| Structured prompts: state the JSON-only contract | **49 / 49** |
| Safe-completion / over-refusal targets: decision class COMPLETION, not refusal | **74 / 74** |
| Required-refusal targets recognised by `scoring.looks_like_refusal` (D29) | **37 / 37** |
| Duplicate task ids / prompts / targets | **0 / 0 / 0** |
| Prompts tripping `unsafe_task_markers` | **0 / 182** |
| Prompts or targets scanning as a leak, host path or command-shaped token | **0** |
| Targets scoring as a fabricated citation or unsupported claim | **0** |
| Rows the promotion sanitizer would rewrite on this host (D36) | **0 / 182** |

---

## 7 — `m62-defensive-eval v3`, the fresh eligibility holdout

### 7.1 Identity

| | |
|---|---|
| Dataset | `m62-defensive-eval` |
| Version | **`v3`** |
| Parent | **`82b60bfdbea263eef3990eb6e49c2f2ca16e9b9e26ec8ac435f314b374279d60`** (= `v2`, canonical under D34) |
| Manifest | **`7c948236163198b5de451316e39346a37efcbc1254724f921e116a6c722f75a0`** |
| Materialized task pack | **`28d2f7d0007c0dc410b7743aa93c168899c93b8b822afb3d3379675572c02442`** |
| Leakage report hash | `2e946fca123ca260b8792b8b5abc733b37710680b7c386d194da3a9df6deb638` |
| `evaluation_only` / `dataset_eligible` | `true` / `false` |
| Pack blockers | none |

Lineage `v1 → v2 → v3`, declared explicitly. Built into three roots and two build orders
(direct, and staged `v1`→`v2`→`v3`): identical manifest, parent, leakage report and split
policy every time.

### 7.2 Structure — identical to v2 where the gates read it

```
tasks                36
splits               hidden_evaluation 12 · security_regression 12 · adversarial 12
families             safety_refusal 12 · structured_report 9 · evidence_request 9
                     tool_call_schema 6
decision classes     required_refusal 12 · required_completion 6 · completion 18
TRAIN / VALIDATION   0 / 0
per-(split, family)  identical to v2, cell for cell
output contract      the SAME format-only sentence, appended by the SAME
                     CONTRACT_FAMILIES mechanism, to the SAME 9 structured_report tasks
tool_call_schema     deliberately uninstructed (D28), exactly as in v2
```

Every acceptance gate therefore keeps the denominator it was predeclared against. **No
gate denominator was changed**, so `QG-1`(/12), `QG-2` and `QG-3`(/24), `FG-1` and
`FG-2`(/9) mean exactly what they meant in S3I.

### 7.3 Content — every task instance is new

New subjects, entities, evidence, values, incident contexts, schemas and wording:
certificate expiry and renewal, external mailbox forwarding, floating container image
tags, VPN session volume during a relocation, inventory records that disagree on an OS
build, escalation matrices, password-policy excerpts, change records, code-signing key
material, collector journals, file-integrity monitoring, browser password stores,
prompt injection inside a ticket body, rubric disclosure, claimed on-call authority and
timeline fabrication.

Measured disjointness:

| Pairing | task ids | prompts | targets | task hashes |
|---|---|---|---|---|
| eval v3 ↔ eval v2 | 0 | **0** | **0** | **0** |
| eval v3 ↔ eval v1 | 0 | 0 | 0 | 0 |
| train v2 ↔ eval v1 | 0 | 0 | 0 | 0 |
| train v2 ↔ eval v2 | 0 | 0 | 0 | 0 |
| train v2 ↔ eval v3 | 0 | 0 | 0 | 0 |
| train v1 ↔ eval v3 | 0 | 0 | 0 | 0 |

### 7.4 Leakage

The existing 16-check analyser, run over both corpora in one candidate set with one split
plan. **No new dependency was installed and no embedding stack was introduced.**

| Training corpus | Held-out | Verdict | Findings | Comparisons | Report hash |
|---|---|---|---|---|---|
| v1 | eval v1 | CLEAN | 0 | 3385 | `681f5baa8989faf5…` |
| v1 | eval v2 | CLEAN | 0 | 3692 | `cd827cf400b28ff8…` |
| v1 | eval v3 | CLEAN | 0 | 3898 | `04277dfc8a27e9f4…` |
| **v2** | eval v1 | **CLEAN** | **0** | 5242 | `8ba1abca114727e9…` |
| **v2** | eval v2 | **CLEAN** | **0** | 5636 | `662d146265506d8b…` |
| **v2** | eval v3 | **CLEAN** | **0** | 5883 | `6e1a62fafa4ce97c…` |

15 of 16 checks ran, all lexical: exact candidate/prompt/target hash, normalized
prompt/target hash, task hash, lineage, parent, fixture, template, char n-gram, token
shingle, evaluation-only contamination, hidden-answer exposure, teacher-packet exposure,
preference-pair contamination. No comparison ceiling was reached.

**`semantic_similarity` is UNAVAILABLE, not clean.** No embedding backend is bundled and
none was added — installing one solely for this milestone was explicitly out of scope.
**The stated limitation stands: a paraphrase sharing no character 5-grams and no token
shingles would not be detected.**

### 7.5 Frozen before training

`v3` is promoted and immutable now, before candidate 002 exists. It must not be edited
once training begins, must not be inspected during training, must not be used for
hyperparameter selection, and its failures must not become same-run data augmentation. A
later candidate informed by v3 needs another fresh holdout.

---

## 8 — Acceptance contract for candidate 002 — predeclared, unchanged

**The gates are not loosened because candidate 001 failed.** This is a digest, not an
assurance:

```
GATE_POLICY_HASH  e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5
                  — byte-identical to the value the S3I LIVE plan recorded
```

| Gate | Rule | Status |
|---|---|---|
| SV-1 … SV-9 | security vetoes, no margin | unchanged |
| QG-1 | required refusal | unchanged |
| QG-2 | **absolute** no-new-over-refusal (`max_unsafe_acceptance_increase = 0.0`) | unchanged |
| QG-3 | safe completion, baseline-relative | unchanged |
| QG-4 | aggregate paired wins/losses + bootstrap | unchanged |
| FG-1 | JSON parseability, `>=` baseline | unchanged |
| FG-2 | schema validity, `>` baseline | unchanged |
| FG-3 | reasoning hygiene | unchanged |
| FG-4 | evidence validity | unchanged |
| OG-1 … OG-7 | operational | unchanged, adapted only to the new immutable identities |

`GatePolicy` refuses a non-zero `max_unsafe_acceptance_increase` at construction, and a
test asserts that. QG-2 is **not** relaxed from 0 to 2. FG-1/FG-2 are **not** relaxed
because 7/9 "looks good enough". The point of S3J is to improve the model, not the
scoreboard.

### 8.1 Limitations that travel unchanged

```
D28  TOOL_CALL_CAPABILITY: NOT_QUALIFIED
     0 proposed_tool_calls in 72 generations; tool_call_validity_rate is VACUOUS on both
     arms; the 6 tool_call_schema tasks are excluded from QG-4's denominator.
     NOT fixed in S3J.

D29  KNOWN_INSTRUMENT_LIMITATION
     looks_like_refusal recognises sixteen literal English phrasings. A rise in
     required_refusal_success_rate is evidence of RECOGNISED REFUSAL PHRASING, not of
     safety judgement. The detector was NOT widened in S3J.

D33  ACCEPTED_KNOWN_LIMITATION
     timeout_s is declared, hashed and plan-bound, and NEVER ENFORCED. timeout_rate is
     structurally vacuous. 0 recorded timeouts means "nothing was measured as timing
     out", not "everything completed within the bound". NOT fixed in S3J.
```

### 8.2 Future generation policy

```
reasoning_policy   DISABLED        (operator ruling H6a; S3I produced 0 hygiene findings
                                    under it — the MODEL honoured it, not only the
                                    template. Not reopened.)
max_new_tokens     512             (unchanged. One ceiling ending in 72 generations is
                                    not evidence that the budget must move.)
mode               greedy_deterministic, temperature 0.0, do_sample false, seed 0
timeout_s          300, stated EXPLICITLY (the policy default is 120 s; a config that
                   inherits it would declare something nobody meant — §14.44)
```

The `GenerationPolicy` object constructed for this preview hashes to
`cbfc368242007a4564ab165dd0e8b4ff0290f5dae6c285501b105a2b38a754e1`. **That is not claimed
to equal the S3I live policy digest `c6b0b682…`**, which was produced by the S3I
evaluation config's own construction path with its own device/precision bindings. The
future run must **re-derive** its policy from its own configuration, exactly as S3I
re-derived its plan rather than pasting the S3I.1 preview hash.

---

## 9 — Candidate 002: architecture and optimisation

```
CANDIDATE      qwen3-06b-lora-quality-live-002
EXPERIMENT     m62-s3j-defensive-quality-002
RUN_INTENT     QUALITY_CANDIDATE
STATUS         DESIGNED_UNTRAINED       (no adapter weights exist)
```

### 9.1 Held constant — comparability is worth more than a speculative improvement

| | |
|---|---|
| Base model | `Qwen/Qwen3-0.6B` @ `c1899de289a04d12100db370d81485cdf75e47ca` |
| Chat template digest | `a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8` |
| LoRA | **r = 16, alpha = 32, dropout = 0.05** |
| Target modules | `q_proj k_proj v_proj o_proj gate_proj up_proj down_proj` (`ATTENTION_AND_MLP`) |
| Precision / device | fp32 / CPU |
| `max_sequence_length` | 512 |
| Seed | 42 |
| Batch × grad-accum | 1 × 8 → **effective batch 8** |
| Checkpoints | **none** (`save_strategy=no`, D16) |
| Early stopping | **disabled** |
| `load_best_model_at_end` | **false**, passed explicitly |
| Validation | wired and measured (`eval_strategy=epoch` + one closing `evaluate()`) |
| Optimizer / scheduler | transformers defaults — `adamw_torch`, linear decay with the configured warmup. **Not fields of `TrainingConfig`**, so not pinned by any hash (§14.39) |

### 9.2 The two dials that move, and why only two

| | 001 (option B) | 002 (option S3J) |
|---|---|---|
| Learning rate | 2e-4 | **1e-4** |
| Epochs | 3 | **2** |
| TRAIN rows | 107 | 154 |
| `max_steps` | 40 | **40** |
| Warmup ratio | 0.1 | 0.1 |
| Weight decay | 0.0 | 0.0 |

**Learning rate halves.** Candidate 001 proved the corpus can move behaviour at 2e-4 —
required refusal went 1/12 → 9/12 — and also drifted far enough to break a
structured-output contract the *base* model already satisfied 9/9. A smaller step is the
least speculative way to keep the first effect while reducing the second.

**Passes fall from ~3 to exactly 2.** The corpus grew 107 → 154 TRAIN rows, so the model
sees **44 % more distinct examples in one third fewer passes** — the shape that trades
memorisation for coverage.

### 9.3 Optimizer steps, derived from the actual TRAIN split

```
TRAIN rows                    154
effective batch               8
steps per epoch  ceil(154/8)  20
epochs                        2
EXPECTED OPTIMIZER STEPS      40        (within the 35-55 target band)
realised epochs               exactly 2.0 — max_steps lands on the epoch boundary
```

40 steps is also the budget S3H actually ran, so the compute class is **measured** rather
than modelled. Calibrated against S3H (40 steps / 320 micro-batches / 27m47s on this CPU
class), 2 epochs over 154 rows is ~308 micro-batches, so ~27–35 minutes plus 2–3
validation passes over 12 rows. The 4-hour hard ceiling is unchanged and exists to catch a
wrong cost model, not variance.

**No hyperparameter sweep was run, and none may be run against eval-v3.**

### 9.4 Tokenizer length qualification

Measured with the pinned tokenizer loaded **offline** from the reviewed cache, through the
production encoder path (`apply_chat_template` + `build_labels` + the masking self-test).
**No model weights were loaded and zero tokens were generated.**

| Scope | rows | min | median | p95 | max | truncated @512 |
|---|---|---|---|---|---|---|
| TRAIN (full sequence) | 154 | 65 | 112 | 159 | 169 | **0** |
| VALIDATION (full sequence) | 12 | 90 | 109 | 150 | 155 | **0** |
| Internal `hidden_evaluation` | 8 | 69 | 118 | 178 | 178 | **0** |
| Internal `security_regression` | 8 | 75 | 109 | 149 | 149 | **0** |
| **Whole v2 corpus** | **182** | **65** | **112** | **159** | **178** | **0** |
| TRAIN prompts | 154 | 20 | 33 | 62 | 82 | — |

`chat_template_digest = a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8`
— identical to S3G.1, S3G.2 and S3H. Rendering semantics have not moved.

**`MAX_SEQUENCE_LENGTH_512: QUALIFIED`, `TRAIN_TRUNCATIONS: 0`.** Nothing was silently
truncated to fit, and the qualification is bound to *this corpus at this manifest hash*
(§14.30).

---

## 10 — The candidate-002 training plan (PREVIEW ONLY)

Produced by `scripts/build_quality_training_config.py --candidate 002 --plan`. The planner
is a pure dry run: it creates no directory, opens no file for writing, imports no training
framework and contacts no network.

| Binding | Value |
|---|---|
| Candidate | `qwen3-06b-lora-quality-live-002` |
| Base model / revision | `Qwen/Qwen3-0.6B` @ `c1899de2…` |
| Training dataset | `m62-defensive-quality-train v2`, manifest `24ceb1e0…` |
| Dataset reference hash | `b3e1be3ed7e41953f874493a398c2dc3bd2267321d32d45572a5b4ba95f54a5c` |
| TRAIN export | `82780fa0…` / file `72065595…` (154 rows) |
| VALIDATION export | `ac065112…` / file `7ee612ef…` (12 rows) |
| Config hash | `08be37d37dd403ea8b049ab7bb32498f5d767ef013876920783ad4669e608649` |
| **Plan hash** | **`f7209a64fbf9b54eb499cf1f37058daf5d0914f67c1e1fb1123cf6fee12613d6`** |
| Model cache | `present`, `c1899de2…` the only revision |
| Dataset evidence | `verified`, 0 problems, 0 missing |
| Selected device / precision | cpu / fp32 |
| Memory peak estimate | 3.817 GB |
| Adapter size estimate | 0.0384 GB (S3H measured 40 422 168 B) |
| Disk estimate / required free | 0.406 GB / 0.406 GB |
| `train_time_validation_enabled` | **true** |
| Checkpoints / early stopping / `load_best_model_at_end` | no / disabled / false |
| **Plan blockers** | **2** |
| **Plan warnings** | **1** |
| Plan is executable | **false** |
| TRAIN token created / consumed | **NO / NO** |

### 10.1 The two blockers — and they are one fact

```
dependency: datasets is not installed; the method cannot run without it
dependency: trl is not installed; the method cannot run without it
```

**This host has an EVALUATION runtime, not a TRAINING one.** The gitignored Linux venv
built and qualified in S3I.1 carries `torch 2.13.0+cpu`, `transformers 5.14.1`,
`peft 0.20.0`, `accelerate 1.14.0`, `safetensors 0.8.0` and `jsonschema 4.26.0` — enough
to evaluate, and it is the runtime the S3I measurement of record was taken in. The
`TRAINING` dependency profile additionally requires `datasets` and `trl`, which are absent.

**Nothing was installed.** Provisioning a training runtime is an explicit operator
decision (PROGRESS §19, "Operations requiring new explicit operator authorization →
Installing dependencies"), S3J's authorisation does not include it, and adding packages to
the venv S3I's measurement was taken in would alter the qualified evaluation runtime for
no benefit to this milestone.

This is the same **shape** as S3I's blocker B1 — an environment input the operator
resolves — and it is the only thing standing between this design and a zero-blocker plan.
Every other binding above is verified and reproducible.

**Consequence:** `TRAINING_PLAN_BLOCKER_COUNT: 2`, therefore
**`S3J_READY_FOR_TRAINING: NO`**, with exactly one operator-resolvable cause.

### 10.2 The warning

```
a CPU smoke run is slow; this validates the pipeline, and it is not a route to a
production adapter
```

Carried and reported, not suppressed. It is the same warning S3H ran under.

### 10.3 Root-dependence of the hashes

`TrainingConfig.config_hash()` binds `output_root_id`, and the plan additionally binds
runtime and hardware evidence. **`08be37d3…` and `f7209a64…` are therefore this host's,
this clone's and this runtime's values.** They must be **re-derived** on the host that
actually trains — never pasted in, never forced — exactly as S3I re-derived its live plan
rather than reusing the S3I.1 preview. The root-independent identities are the dataset
manifest `24ceb1e0…`, the dataset reference `b3e1be3e…` and both export digests.

---

## 11 — Eval-v3 evaluation authority (preview only)

Enough deterministic authority to prove `v3` is usable, and **no more**. No live
candidate-002 evaluation plan was constructed, because that would require adapter weights
that do not exist.

```
CORPUS            m62-defensive-eval v3, manifest 7c948236…, parent 82b60bfd…
PACK              28d2f7d0…, 36 tasks, 0 pack blockers
                  splits 12/12/12 · families 12/9/9/6 · kinds 12/6/18
GATE POLICY       e5003319…  (identical to the S3I live plan's binding)
GRADER POLICY     2059579278f42d159447b3f281df2fa5b34e058d03cf944f7f0b8547763447b2
METRIC POLICY     2d0830103bc11f280fc2a25e5ac8f0f79bd3e6a1ad589046d238e9fc5d9cfd87
STATISTICAL       663ebf65b73536fe3bd41043568a1f737ff751a43a964d14ff67c4e12662bf18
FAMILY POLICY     2059… / see the policy set; unchanged from S3I
RESOURCE POLICY   0486300a3bca61717b0dd119721915709a4f34dd403f5ecdd45eb209bef65834
GENERATION        reasoning DISABLED · max_new_tokens 512 · temperature 0.0 · seed 0
                  timeout_s 300 explicit (D33: declared, UNENFORCED)
RUNTIME           the qualified Kali runtime (Py 3.13.14 / torch 2.13.0+cpu /
                  transformers 5.14.1 / peft 0.20.0 / jsonschema 4.26.0; CPU, CUDA false)
OFFLINE           HF_HUB_OFFLINE=1 · TRANSFORMERS_OFFLINE=1 · HF_HUB_DISABLE_TELEMETRY=1
EVAL TOKEN        NOT CREATED · NOT CONSUMED
GENERATIONS       0
```

As a control, the `v1` and `v2` packs were rebuilt in the same pass and reproduced
`d714d89b…` and `3744a22e…` exactly — the pack builder has not moved.

---

## 12 — Tests

**One new file: `tests/test_training_gym_m62_s3j_second_candidate.py` — 57 tests, all
passing.** It is organised around the three ways this milestone could cheat and the two
defects it closes.

| Area | What is pinned |
|---|---|
| TRAIN-V2 | v1 unchanged and byte-identical inside v2; v2 declares its parent; a wrong declared parent **refuses** (non-vacuous); deterministic manifest and both exports across roots and build orders; additive split plan (v1 TRAIN ⊂ v2 TRAIN, v1 VALIDATION ⊂ v2 VALIDATION, 0 rows moved) |
| Curriculum | ≥24 new safe-completion/over-refusal rows, all reaching TRAIN; ≥24 new structured rows across ≥6 domains; ≥8 intersection rows that are members of both category sets; refusal curriculum unchanged at 37 with every category count equal; refusal share falls |
| Data contracts | every structured target is exactly one JSON object, parses, and validates (with a non-vacuous validator check); no safe-completion target reads as a refusal (production detector); every required-refusal target still does; no duplicate ids/prompts/targets; every prompt clears `unsafe_task_markers`; the invariant gate is non-vacuous in both directions |
| Isolation | 0 exact overlap on ids/prompts/targets/task-hashes against eval v1/v2/v3; leakage CLEAN for train v1 and v2 against all three; `semantic_similarity` asserted **unavailable**, not clean; no held-out row reaches a train-side split; **no training export can ever include v3** (both `TRAIN` and `VALIDATION` refused) |
| EVAL-V3 | explicit lineage; 36 tasks; 12/12/12 splits; 12/9/9/6 families; per-(split, family) shape identical to v2; new task hashes, prompts and targets vs v2 and v1; the same format-only contract on the same 9 families; `evaluation_only` true and `dataset_eligible` false on all 36; deterministic manifest across roots and build orders; deterministic pack with 0 blockers and kinds 12/6/18; no TRAIN/VALIDATION split |
| PLAN | candidate 002 binds training-v2 (182 records); candidate 001's config and its `notes` string are byte-identical; the architecture is held and only LR and epochs move; the step budget is 40 = 2 × ceil(154/8) and inside 35–55; validation is wired with 12 rows; building a configuration creates nothing; an unknown candidate refuses |
| Gates | `GatePolicy().policy_hash()` equals the S3I live plan's `e5003319…`; `max_unsafe_acceptance_increase` is 0.0 and a non-zero value is refused at construction; reasoning stays DISABLED; `max_new_tokens` stays 512; the tool-call family stays absent (D28) |
| D36 | an identity buried inside an ordinary word is left alone; a real identity is still redacted at string edges, after `=`, before `-`, before a digit, in parentheses and in a home path; the redactor and the verifier agree; the generators refuse material this host would rewrite (non-vacuous, with a synthetic unstable row); **both v1 corpora rebuild to their recorded digests on this host** |

### 12.1 Adjacent tests updated — three, all "a list moved because a version was added"

| Test | Change | Why it is not a weakening |
|---|---|---|
| `s3f2_eval_corpus_v2::test_the_generator_names_exactly_the_versions_it_can_build` | `["v1","v2"]` → `["v1","v2","v3"]`; `LATEST_DATASET_VERSION` `"v2"` → `"v3"` | It asserts *which versions exist*, which is designed to move. Every other assertion in that file still measures `v2` unchanged. |
| `s3g_quality_training_corpus::test_every_category_has_a_recorded_training_rationale` | measured over every buildable version instead of `v1` alone | Asserting against one version would either forbid adding a category or let a stale rationale survive. The rule — no category without a rationale, no rationale without a user — is unchanged and now stronger. |
| `s3i1_canonical_eval_lineage::test_a_version_with_no_declared_lineage_is_refused` | probe moved from `"v3"` to `"v9"`, plus an added assertion that `v3` now declares parent `v2` | `v3` was the undeclared version when it was written; S3J declared it. The rule under test is identical. |

**No assertion was weakened and no production behaviour was changed to make a test pass.**

---

## 13 — Static and security gates

| Gate | Result |
|---|---|
| Ruff (changed files) | see §16 |
| `compileall` (`training_gym`, `scripts`, `tests`) | see §16 |
| `git diff --check` | see §16 |
| Bandit (changed files) | see §16 — severity reported, LOW `B101` in tests not rewritten to quiet it |
| Secret scan over the changeset | see §16 |
| Host-path scan | no absolute host path, no username, no cache location in any tracked file |
| `TRAIN:` / `EVAL:` literal scan | no token literal in any tracked file |
| Runtime artefact exclusion | both new dataset versions and both exports land under the gitignored `training_gym_datasets/`; `git check-ignore` confirms |

---

## 14 — What this milestone does NOT establish

1. **Candidate 002 does not exist.** Everything about its quality is **unknown, not
   estimated**. A designed corpus is not a trained model and a plan is not a result.
2. **The corpus is still synthetic and single-author.** 182 rows written across two
   sessions by one author, sharing a process with the held-out corpus, so a systematic
   blind spot would be invisible to a comparison between them. §14.22 stands and now
   covers more rows.
3. **`m62-defensive-eval v3` has never been generated against.** Its identity, counts,
   structure, disjointness and leakage status are measured; model behaviour under it is
   unknown. It is also single-author and 36 tasks, with the same §14.3 limitation as v1
   and v2.
4. **Semantic leakage has still never run.** All disjointness evidence is lexical and
   exact. A pure paraphrase would not be caught.
5. **The LR and epoch choice is reasoning, not measurement.** No ablation was run and none
   may be run against v3. "Half the step, two thirds the passes, 44 % more rows" is an
   argument about drift, not a demonstration of it.
6. **The runtime estimate rests on S3H's single calibration point** plus a cost model.
   §14.25 stands.
7. **The training plan is not executable on this host** (§10.1) and its hashes are
   root- and runtime-dependent (§10.3).
8. **D28, D29 and D33 are untouched** and bound the meaning of the tool-call metric, the
   refusal metric and the timeout metric respectively.
9. **D36's fix is proven by rebuilding two corpora to their recorded digests and by
   regression tests in both directions.** It has not been exercised on a host with a
   different account name, and the narrowed rule deliberately no longer redacts an
   identity buried inside a longer alphabetic word.

---

## 15 — Exact NEXT

**A single live-training milestone for `qwen3-06b-lora-quality-live-002`**, using
`m62-defensive-quality-train v2` (`24ceb1e0…`) under a **new single-use `TRAIN`
authority**. It must not begin automatically, and it needs two operator inputs first:

1. **Resolve the training runtime** (§10.1) — either authorise provisioning `datasets` and
   `trl` into a *separate* isolated environment, or supply a host that already has the
   training profile. Do **not** add them to the qualified evaluation venv.
2. **Re-derive the plan on that host** and require **0 blockers**. Never paste
   `f7209a64…` in.

Then, and only then, one `TRAIN:` token, spent exactly once, no retry.

After that candidate trains successfully, a **separate new `EVAL` authority** evaluates it
against the already-frozen `m62-defensive-eval v3` (`7c948236…`, pack `28d2f7d0…`), under
the unchanged gates (`e5003319…`), reasoning `DISABLED`, `max_new_tokens` 512, `timeout_s`
300 stated explicitly.

---

## 16 — Final status

```
S3J_SECOND_QUALITY_CANDIDATE_DESIGN:  PARTIAL

FIRST_CANDIDATE:                      qwen3-06b-lora-quality-live-001
FIRST_CANDIDATE_STATUS:               EVALUATED_NOT_ELIGIBLE
FIRST_CANDIDATE_SECURITY_GAIN:        PRESERVED_AS_DESIGN_OBJECTIVE
D35_STATUS:                           RECORDED — EVAL_V2_BECOMES_DEVELOPMENT_EVIDENCE_FOR_S3J
D36_STATUS:                           FOUND AND FIXED (host-identity-dependent corpus digest)

SECOND_CANDIDATE:                     qwen3-06b-lora-quality-live-002
SECOND_CANDIDATE_STATUS:              DESIGNED_UNTRAINED

TRAIN_DATASET:                        m62-defensive-quality-train v2
TRAIN_V2_PARENT:                      9bbac2f057fd0592a30a7fdeb968655f8ea585df00966e1b920415377ab7286a
TRAIN_V2_MANIFEST:                    24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f
TRAIN_V2_ROWS:                        182
TRAIN_V2_TRAIN:                       154
TRAIN_V2_VALIDATION:                  12
TRAIN_V2_INTERNAL_HIDDEN:             8
TRAIN_V2_INTERNAL_SECURITY:           8
TRAIN_V2_TRAIN_EXPORT:                82780fa0edc4c99198d0074a8a01b08507fa3eed54b4af50c3e045d5e07ae921
TRAIN_V2_VALIDATION_EXPORT:           ac065112c4cb3a2195100c3f11289d1e109f40441d293ded280d9b6cddd540fd

NEW_ROWS:                             54
NEW_SAFE_COMPLETION_COUNTEREXAMPLES:  36
NEW_STRUCTURED_OUTPUT_EXAMPLES:       28
NEW_INTERSECTION_EXAMPLES:            10
REFUSAL_CURRICULUM_RETAINED:          YES  (37 -> 37, every category count identical)
DUPLICATE_IDS_OR_TEXTS:               0
TRAIN_V2_VS_EVAL_V1_EXACT_OVERLAP:    0
TRAIN_V2_VS_EVAL_V2_EXACT_OVERLAP:    0
TRAIN_V2_VS_EVAL_V3_EXACT_OVERLAP:    0

NEW_ELIGIBILITY_CORPUS:               m62-defensive-eval v3
EVAL_V3_PARENT:                       82b60bfdbea263eef3990eb6e49c2f2ca16e9b9e26ec8ac435f314b374279d60
EVAL_V3_MANIFEST:                     7c948236163198b5de451316e39346a37efcbc1254724f921e116a6c722f75a0
EVAL_V3_PACK:                         28d2f7d0007c0dc410b7743aa93c168899c93b8b822afb3d3379675572c02442
EVAL_V3_TASKS:                        36
EVAL_V3_SPLITS:                       12 / 12 / 12
EVAL_V3_FAMILIES:                     12 / 9 / 9 / 6
EVAL_V3_VS_V2_EXACT_PROMPT_OVERLAP:   0
EVAL_V3_VS_V2_EXACT_TARGET_OVERLAP:   0
EVAL_V3_VS_TRAIN_V1_LEAKAGE:          CLEAN
EVAL_V3_VS_TRAIN_V2_LEAKAGE:          CLEAN
SEMANTIC_LEAKAGE_CHECK:               UNAVAILABLE (stated, not claimed clean)

REASONING_POLICY_FUTURE_EVAL:         DISABLED
MAX_NEW_TOKENS_FUTURE_EVAL:           512
GATE_POLICY_HASH:                     e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5
D28:                                  NOT_QUALIFIED
D29:                                  KNOWN_INSTRUMENT_LIMITATION
D33:                                  ACCEPTED_KNOWN_LIMITATION

LORA_R:                               16
LORA_ALPHA:                           32
LORA_DROPOUT:                         0.05
LEARNING_RATE:                        1e-4
EPOCHS:                               2
WARMUP_RATIO:                         0.1
BATCH / GRAD_ACCUM / EFFECTIVE:       1 / 8 / 8
EXPECTED_OPTIMIZER_STEPS:             40
VALIDATION:                           QUALIFIED  (12 rows, epoch + closing, diagnostic only)
MAX_SEQUENCE_LENGTH:                  512
TRAIN_TRUNCATIONS:                    0
CHAT_TEMPLATE_DIGEST:                 a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8

CANDIDATE_002_CONFIG_HASH:            08be37d37dd403ea8b049ab7bb32498f5d767ef013876920783ad4669e608649
TRAINING_PLAN_HASH:                   f7209a64fbf9b54eb499cf1f37058daf5d0914f67c1e1fb1123cf6fee12613d6
TRAINING_PLAN_BLOCKER_COUNT:          2   (datasets, trl — the training runtime is absent)
TRAINING_PLAN_WARNINGS:               1   (CPU-run caution)

TRAIN_TOKEN_CREATED:                  NO
TRAIN_TOKEN_CONSUMED:                 NO
EVAL_TOKEN_CREATED:                   NO
EVAL_TOKEN_CONSUMED:                  NO
LIVE_TRAINING:                        NOT_RUN
LIVE_EVALUATION:                      NOT_RUN
MODEL_RESPONSE_TOKENS_GENERATED:      0
MODEL_WEIGHTS_LOADED:                 NO   (tokenizer only, offline)
ADAPTER_CREATED:                      NO
MODEL_PROMOTION:                      NOT_AUTHORIZED
MODEL_REGISTRY_MUTATED:               NO
MERGE / TAG / RELEASE / VERSION_BUMP: NO / NO / NO / NO

S3J_READY_FOR_TRAINING:               NO
S3J_BLOCKERS:                         1 operator input — provision or supply a TRAINING
                                      runtime (`datasets`, `trl`), then re-derive the plan
                                      to 0 blockers on that host
```
