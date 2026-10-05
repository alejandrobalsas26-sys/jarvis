# V69 · M67A.1 — REAL LEGACY REASONING CORPUS QUALIFICATION

Development milestone. **No training, no model adaptation, no scientific evaluation, no
promotion, no external benchmarking.** `eval-v7` stays `USED_IMMUTABLE`; `candidate006` and
`eval-v8` remain absent; zero evaluation authority was spent.

This document is the tracked receipt. It carries **privacy-safe aggregates only**: no
conversation bodies, no message excerpts, no filenames, no paths, no per-item content
hashes, no identities. The corpus itself and every derived artifact stay under an
ignored private root outside this repository.

---

## 1. What this milestone established — and what it did not

**Established.** The `.docx` export format is now *decidable*: a Word-saved chat that
carries role labels is ingested correctly, and one that carries none is refused with the
structural counts that justify the refusal. The §7 prohibition on hidden-reasoning
authority fields is now **enforced by tests** rather than holding by construction.

**NOT established.** The supplied corpus **did not qualify**. Zero conversations were
ingested, zero families formed, zero procedures distilled, and no frozen holdout exists.
Nothing here says anything about reasoning quality, model behaviour, or future performance.

The honest summary is one sentence: *the software is ready and the format is understood;
the material that was supplied is not a conversation corpus.*

---

## 2. Starting state — verified, not trusted

| Property | Value |
|---|---|
| `master` == `origin/master` | `3e4b396e3d9211b5041826e860ab97951ba8f5f0` |
| Integrated milestone | `M68C_INTEGRATED_AND_VERIFIED` |
| Control plane | `m62.control_plane.4`, generation **66**, **PASS / 0 problems** |
| Gen-66 snapshot | `0066-m68c-patch-execution.json`, digest recomputed and matching |
| `master-anti-rewrite` ruleset `22789465` | active (`deletion`, `non_fast_forward`) |
| Authoritative baseline (pristine) | **12592 passed · 48 skipped · 0 failed**, collection **12640** |
| Scientific baseline (pristine, run alone) | **3179 passed · 2 skipped · 0 failed**, collection **3181** |

Both baselines were measured **before any tracked edit**, on CPython 3.11.16 with
`constraints-ci`, using the CI-authoritative invocation from the repository root.

---

## 3. The supplied corpus — structure only

One source file. Inspection was structure-first: ZIP member listing, paragraph counts,
style histogram, length distribution, and a marker match against the closed role table.
Bodies were not dumped; a bounded prefix sample was used **once**, to establish the
document's *type*, which decides whether an adapter is even the right tool.

| Measurement | Value |
|---|---|
| Source files | **1** |
| Observed formats | **1** (`.docx`, WordprocessingML) |
| Declared size | 155,826 bytes |
| ZIP members | 13 |
| Body member | `word/document.xml`, 179,801 bytes uncompressed |
| `w:p` paragraphs | **286** (281 non-empty, 5 empty) |
| `w:pStyle` elements | **0** |
| Paragraphs matching the closed role table | **0** |
| Distinct roles recoverable | **0** |
| Embedded drawings / images | 1 (one PNG, not ingested) |
| Tables, hyperlinks, comments, bookmarks | 0 |
| Distinct paragraphs | **227** of 281 — 34 repeat, **54 redundant copies**, max repeat 3 |
| Paragraphs ending in a question mark | **0** |
| Word-reported pages / words | 44 / 17,043 |

### What the structure means

Three independent signals say the same thing:

1. **No style markup at all.** Word stored no `w:pStyle`, so there is no structural
   channel that could separate a user turn from an assistant turn.
2. **No textual role markers.** Zero of 281 paragraphs match the closed label table.
3. **No user side.** Not one paragraph ends in a question mark, and ~40% open with
   first-person narration. The content is *assistant reasoning prose* — there are no
   requests and no answers.

Topics change completely across the run (directory-service promotion, kernel tracing
hooks, a Spanish-language curriculum, performance tables), with **no delimiter of any
kind** between them. So the document is not one conversation; it is fragments of many,
concatenated, and **the number of conversations it contains is UNKNOWN**.

This **confirmed** M67A's original reason for leaving `docx` unimplemented rather than
refuting it: *the role structure of a Word-saved chat is layout, not markup.*

---

## 4. The `.docx` adapter — minimal, deterministic, and it refuses

`reasoning_distillation/adapters/docx_export.py`, version `m67a1.docx.1`.

Extraction is deterministic: `w:p` in document order, `w:t` for text, `w:tab`/`w:br` for
layout whitespace, and the **closed label table imported from the transcript adapter** so
one table serves both containers and cannot drift between them.

### Why a refusal and not 281 `UNKNOWN` turns

The base contract provides for a segment whose role cannot be established: it becomes
`UNKNOWN` and the conversation becomes `NEEDS_HUMAN_REVIEW`. That provision is for one
turn inside an otherwise-structured conversation. Applying it to a document with no
structure at all would assert something the source does not say:

> **a boundary-less paragraph run has no conversation identity.**

Importing it as one conversation claims it *is* one conversation. §7 forbids synthesising a
conversation id and presenting it as source truth, and "one file, therefore one
conversation" is exactly that synthesis — positively contradicted here by the topic
changes. The transcript adapter already made this call for the same reason, refusing rather
than importing "one unlabelled blob". The container is irrelevant to the question.

So the adapter ingests when the table finds **≥ 2 distinct roles** and refuses otherwise,
naming every count that drove the decision plus the route forward.

### What the adapter deliberately does not read

| Not read | Why |
|---|---|
| `docProps/core.xml` | carries the name of the human who saved the file; §4 forbids user identifiers in derived data, and metadata is a side door |
| `dcterms:created` as `source_date` | that is when **Word** saved, which for a pasted chat is when the pasting happened — a confident wrong conversation date, the same trap as file mtime |
| `w:delText` | content a human **deleted** under tracked changes; including it would resurrect removed text and attribute it to the conversation |

`source_model` and `source_date` therefore stay `""` — UNKNOWN, never inferred.

### Hardening

Parsing is `defusedxml` (no DTDs, entities or external references), **fail-closed** on its
absence — there is deliberately no stdlib fallback, because a fallback would silently
remove the hardening. A **declared-size bound** is checked against the ZIP member's
uncompressed size *before* decompression, so a compression bomb never reaches the parser.
Both follow the convention already established by `tools/sysmon_bridge.py`.

---

## 5. Residual closed — hidden-reasoning authority fields

§7 is an epistemic rule: visible historical rationale is **source material**, not
privileged hidden reasoning, and no field may be named `chain_of_thought`. At M67A the rule
held *by construction* — the name appeared nowhere — but nothing pinned it. The existing
forbidden-token scan covers `candidate006`, `eval-v8`, `TRAIN:`, `EVAL:` and `PROMOTE:`,
and **not** that name.

M67A.1 is precisely when that gap stopped being theoretical: the supplied material is 44
pages of visible "thinking" prose, which is the most tempting thing in this project to
mislabel, because superficially it looks like one. It is not — it is a visible, exported,
operator-supplied summary of unknown provenance, and naming it as hidden reasoning would
both claim access to model internals this project does not have and convert prose into
ground truth by an act of naming.

The control is a closed denylist of **26 names** that assert hidden-reasoning authority,
matched **exactly** on the normalised identifier (lowercase alphanumerics), enforced three
ways: an AST sweep of every package source, a live sweep of every dataclass field, and a
live sweep of every enum member and value. Exactness is load-bearing — a substring match
would reject the legitimate `assistant_reasoning` and `reasoning_relevance` that the real
schema needs in order to describe what it holds.

Every detector carries a **non-vacuity witness**, and nine legitimate names are asserted
*not* to trip it.

---

## 6. Real corpus run — the measured result

Run under one policy over the declared root; no hand-picking.

| Aggregate | Value |
|---|---|
| Source files seen | 1 |
| Unsupported / refused | **1** |
| Logical conversations | **0** |
| Messages / turns | **0** |
| Exact duplicates | 0 (nothing ingested to deduplicate) |
| Families | **0** |
| Development / review / frozen-holdout families | 0 / 0 / 0 |
| Secret quarantine · private exclude · review · rejected | 0 · 0 · 0 · 0 |
| Qualified | **0** |
| Distilled procedures | **0** |
| Split defensible | **false** |
| Frozen holdout | **none created** |

The pipeline's own refusals, verbatim in substance:

- split shortfall — *"the corpus is empty: no family to place, and no defensible holdout exists"*
- stage not run — *"freezing an empty holdout would record that one exists"*

**That refusal is the most important result in this milestone.** The tempting alternative —
import the paragraphs, form one family, carve a 15% holdout — would have produced a
corpus-shaped artifact with no corpus in it, and every downstream number would have been
real-looking and meaningless.

Manifest declarations, all `false`: `training_performed`, `evaluation_spent`,
`raw_corpus_committed`, `holdout_read_by_any_stage`, `implies_promotion_eligibility`,
`production_reasoning_changed`.

### Source immutability and determinism

The source export's digest, byte size and mtime are **identical** before and after
processing. Two consecutive builds over unchanged input produced the **same structural
digest and the same build id**.

---

## 7. Why the corpus does not qualify

A chain of four facts, each measured rather than assumed:

1. The single export has **no recoverable role structure** → it cannot be ingested without
   fabricating roles.
2. Nothing ingested → **zero families**.
3. Zero families is below the configured floor of **24 families** for any defensible
   holdout → the split refuses, by design, rather than carving one.
4. No holdout and no eligible development material → **no procedures can be distilled**.

The configured floor was **not** lowered to improve yield. §21 is explicit that real-data
failure is evidence, and lowering a threshold in the same session that the data failed it
is the post-hoc weakening the repository's science rules exist to prevent.

**Yield is not the objective. Truthful qualification is.** This corpus yielded nothing, and
that is a correct outcome rather than a malfunction.

---

## 8. One superseded assertion, rescoped

`test_deliberately_absent_formats_name_what_is_needed_to_add_them` asserted §5's property
("do not implement an adapter because it could theoretically exist") *through* `docx`. A
real sample then arrived, `docx` left the absent list and gained an adapter, so the old body
would have asserted that a format **with** an adapter has none.

The invariant is unchanged; only its witness moved to `html`, which carries the identical
property and still has no sample. A second test pins the other half — that the list no
longer claims `docx` is absent, that the adapter is registered, and that `html` still is.
Rescoped assertions are not regressions, and each rescoping is argued where it happens.

---

## 9. Tests added

| Suite | Count | Covers |
|---|---|---|
| `test_reasoning_docx_adapter_m67a1` | 33 | sniff, both ingestible shapes, the role-less refusal, no fabrication, malformed/hostile input, the size bound, fail-closed XML, determinism |
| `test_reasoning_hidden_reasoning_prohibition_m67a1` | 26 | the §7 denylist, AST + dataclass + enum sweeps, 9 non-vacuity witnesses, 9 legitimate-name guards |
| `test_reasoning_real_corpus_invariants_m67a1` | 22 | the real outcome reproduced synthetically, empty-corpus holdout refusal, idempotence, source immutability, privacy through the new container, tracked-text leak scans |

**82 added** (the three suites above plus one in the rescoped M67A file), over the
203 M67A tests, all green. Every fixture is synthetic: structure
copied, bodies never. Authoritative collection **12640 → 12722**.

### Two controls that were wrong before they were right

Both are recorded because a control whose first real finding is a false positive teaches the
next reader to override it:

1. A leak scan matching any absolute home-directory prefix followed by a user segment
   fired on an **illustrative container path** in M66B's volume table — a service account,
   not a person.
2. A scan for this operator's *own* home path fired on M66B's capability matrix, then on
   `core/containment.py` and the escape-matrix tests — where the real home path is
   **load-bearing security configuration**, because the sandbox proves the host home is
   absent inside the jail and cannot do that without naming it.

"An absolute path exists" is not the hazard. The hazard is a path pointing at **corpus**
data, so the control is scoped to the private derived root, derived at runtime so that no
username is written into tracked test source.

---

## 10. Falsification campaign

`scripts/mutation_campaign_m67a1.py`, run from `jarvis/`. One mutation at a time, an exact
anchor required to occur exactly once, a fresh test process per mutation, always restored.
Every mapped test is run **unmutated first** — a red or skipped mapping would report
DETECTED for everything and make the campaign a rubber stamp.

```
mutations 29 · detected 28 · explained 1 · UNEXPLAINED 0 · anchor errors 0 → PASS
```

| Category | Mutations | What each tries to weaken |
|---|---|---|
| `F_FORMAT` | 10 | claim any ZIP, accept one role, accept a role-less document, default a missing role to *user*, accept an empty document, treat an unknown format as merely absent, re-add `docx` to the absent list, invert registry order, blank the shared label table, drop `.docx` from the ingest allowlist |
| `P_PROVENANCE` | 8 | fabricate `source_date` / `source_model`, stop publishing the stage version, force review status clean, drop the style caveat, make the style caveat fire unconditionally, drop the attachment caveat, reverse document order |
| `V_PRIVACY` | 1 | resurrect tracked-change deletions into the corpus |
| `X_HARDENING` | 2 | remove the zip-bomb bound, replace fail-closed XML with a stdlib fallback |
| `Z_HIDDEN` | 3 | declare a hidden-reasoning field, smuggle one in as a mapping key, offer one through the published contract |
| `O_HOLDOUT` | 5 | declare an empty corpus defensible, remove the family floor, bypass it, randomise placement, drop the seed from placement |

### The two survivors, and what they taught

Both first-round survivors are recorded rather than quietly removed.

**`O_split_seed_unstable` — my mutation was wrong, not the code.** It set the
`SplitConfig.seed` dataclass *default* to `random.randint(...)` and survived. The reason is
worth keeping: a dataclass field default is evaluated **once, at class creation**, so every
build inside one process shared the same random seed and no digest moved. The mutation was
semantically inert. It was replaced by two that mutate `placement_score`, which is called
per family per build — and both are DETECTED.

Replacing it also exposed a real gap. The stability assertion had to be written against
`placement_score` directly, because the real corpus produced **zero** families and a
one-family corpus forces everything into development: neither exercises placement at all,
so a corpus-level stability test would have passed while placement was entirely random.

**`O_holdout_always_possible` — an EXPLAINED masked survivor, retained.** Setting
`holdout_possible = True` changes no outcome, because

```
holdout_available = holdout_possible AND holdout_count >= min_families_per_partition
```

ANDs two independent floors. Measured at `frozen_holdout_ratio` 0.15: the per-partition
floor of 8 needs **~54 families**, while `min_families_for_holdout` needs **24**. So for
every family count the partition floor decides first:

| families | total floor (24) | partition floor (8 of 15%) |
|---|---|---|
| 24 | OK | **BLOCK** (≈3 in holdout) |
| 40 | OK | **BLOCK** (≈6) |
| 50 | OK | **BLOCK** (≈7) |
| 54 | OK | OK (≈8) |

`min_families_for_holdout = 24` is therefore **dead configuration** — no input exists for
which mutating it alone is observable. It was **not retuned here**: changing a threshold is
a policy decision, and this session has no corpus that would justify one. Recorded as a
residual instead.

The campaign's pass condition is **0 unexplained survivors**, and an entry may only be
explained with a measurement showing no input distinguishes mutated from unmutated
behaviour. "I could not find a test for it" is not an explanation.

---

## 11. Boundaries honoured

| Control | State |
|---|---|
| Training / optimizer / LoRA / weights | none. The package imports no model or network library |
| `candidate006` · `eval-v8` | ABSENT (prohibition text only) |
| `eval-v7` | `USED_IMMUTABLE`, not reopened, not spent |
| Scientific suite | re-run as regression only; collection unchanged at 3181 |
| Frozen scientific / training trees | byte-unchanged |
| Holdout spend | zero; no stage reads frozen-holdout content |
| External model/API calls | none. The pipeline is offline and provider-independent |
| Raw or derived corpus data in Git | none. Verified by `git check-ignore`, by a tracked-file sweep, and by an unchanged worktree |
| Production `TaskDecision` behaviour | unchanged; the package is additive |
| `master` | untouched |

Semantic distillation was **not reached** — it is blocked upstream by an unqualifiable
corpus, not by missing authority. No separate authority was requested or needed.

---

## 12. Residual limitations

1. **The corpus is unqualified and this milestone does not make it usable.** Converting the
   material would mean reconstructing roles and conversation boundaries that the export
   does not contain. That is authoring, not ingestion, and it is out of scope here.
2. **`.docx` ingestion has never been exercised against a real labelled Word export.** The
   ingestible path is proven on synthetic fixtures only; the one real sample took the
   refusal path. Parse coverage for real labelled `.docx` remains **unmeasured**.
3. **`html` and `pdf` remain deliberately absent**, each still naming what a sample would
   need to supply.
4. **The hidden-reasoning denylist is nominal.** It catches a field that *claims* hidden
   authority by name. It cannot catch a legitimately-named field whose contents are later
   treated as ground truth by a consumer; that remains a review property.
5. **The embedded image was not ingested.** Any reasoning in the source that depends on its
   content is incomplete, and the adapter reports this rather than ignoring it.
6. **Dedupe, family grouping and the split are unexercised on real data** — they ran, and
   correctly produced zero, but zero exercises a refusal path rather than the grouping
   logic.
7. **`min_families_for_holdout = 24` is dead configuration.** The per-partition floor
   dominates it at every family count; the effective threshold for any frozen holdout is
   **~54 families**, not 24. Two thresholds where one is unreachable means the documented
   number is not the operative one. Left as measured, for a policy decision with data
   behind it.
8. **Placement stability is asserted at the function, not end-to-end.** No corpus in this
   milestone had enough families for a partition-level reshuffle to be observable, so
   `placement_score` is tested directly. End-to-end holdout stability across reruns of a
   real multi-family corpus remains **unmeasured**.

---

## 13. What a future milestone would need

To qualify real material, the operator supplies conversations that retain **role structure**
and **conversation boundaries** — either the `m67a.conversation.1` JSON contract, or
role-prefixed transcripts using the closed label table, or Word exports that preserve those
labels. A corpus of at least **24 families** is required before any defensible frozen
holdout exists.

None of that is a training authority, and a qualified corpus would still not be one.
