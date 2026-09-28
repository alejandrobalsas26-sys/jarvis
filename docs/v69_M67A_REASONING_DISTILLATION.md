# V69 M67A — Reasoning Distillation Foundation

**Status:** infrastructure complete, no real corpus ingested.
**Terminal status:** `M67A_INFRASTRUCTURE_READY_CORPUS_INPUT_REQUIRED`

M67A builds the machinery to turn historical AI conversations into a trusted corpus of reusable
**decision procedures**. It ingests nothing real: no corpus path was supplied, so this milestone
delivers the deterministic foundation, the firewall, the bounded distillation loop, the quality
gate, the skill layer, the benchmark harness and a synthetic test corpus — and stops at the input
contract.

---

## 1. What M67A may claim

Historical reasoning traces can be:

- ingested with deterministic provenance
- normalized into a typed conversation model
- privacy-classified with an explicit, fail-closed export status
- deduplicated exactly and near-exactly, into problem families
- split by family without known leakage
- separated from factual truth
- transformed into typed decision procedures
- recursively critiqued under bounded rules
- source-verified by per-field attribution
- quality-gated across fifteen independent dimensions
- converted into evidence-backed explicit reasoning skills
- prepared for a controlled future evaluation

## 2. What M67A must NOT claim

- ❌ "JARVIS thinks like Claude"
- ❌ "Claude's reasoning was replicated"
- ❌ "reasoning improved"
- ❌ "the corpus is unbiased"
- ❌ "the traces are ground truth"
- ❌ "training will improve the model"

Each needs evidence this milestone deliberately did not gather. Nothing in the package produces a
number that could imply promotion eligibility.

## 3. Science freeze — what was not touched

| Control | State |
|---|---|
| Model training | none. No `torch`, `transformers` or adapter import anywhere in the package |
| LoRA / optimizer / weights | unchanged |
| `candidate006` | ABSENT (appears only as prohibition text in prior records) |
| `eval-v8` | ABSENT (`eval-v7` remains the ceiling, `USED_IMMUTABLE`) |
| TRAIN / EVAL / promotion authority | none requested, none spent |
| Holdout spend | zero. No stage reads frozen-holdout content |
| Production `TaskDecision` behaviour | unchanged. The package is additive and nothing imports it |

`test_no_stage_reports_an_evaluation_result_or_a_promotion_signal` and
`test_no_module_in_the_package_imports_a_model_or_network_library` assert these against the build's
own output rather than restating them.

---

## 4. Architecture — reuse, not a parallel brain

§3 forbids building a "ClaudeBrain". The distilled corpus maps into JARVIS's existing decision
architecture, and the vocabulary is imported rather than re-declared:

| M67A need | Existing JARVIS abstraction reused |
|---|---|
| canonical JSON, hashing, validators, schema versioning | `training_gym.schemas` |
| body-free representations | `training_gym.schemas.body_free_repr` |
| secret / PII scanning | `training_gym.schemas.scan_private_content` → `core.redaction_policy`, `core.memory_router` |
| deterministic near-duplicate scoring | `training_gym.datasets.similarity` (thresholds inherited) |
| family grouping, split refusal semantics | patterned on `training_gym.datasets.split` |
| **decision vocabulary** | `core.epistemic_deliberation`: `IntentGoal`, `FreshnessRequirement`, `PremiseState`, `ToolNeed`, `ToolExecutionState`, `DeliberationMode`, `VerificationStatus` |
| claim provenance / status | `core.mesh_contracts.Provenance`, `ClaimStatus` |
| export sensitivity | `training_gym.schemas.SensitivityClass` |
| provider availability / kind | `training_gym.teachers.base.TeacherAvailability`, `TeacherKind`, `ReviewMode` |

`TeacherProvider` itself is **not** subclassed: its `_produce` takes a teacher packet bound to the
training gym's episode model, none of which exists here. A narrow `DistillationProvider` Protocol
is defined instead, carrying the same authority ceiling — a provider returns *suggestions*, its
critique findings are ADVISORY-capped, and it can never accept or reject a record.

16 of the 26 reasoning operators map onto a named JARVIS decision field. The remaining 10 are
recorded as **unmapped** rather than forced onto the nearest field — a forced mapping would make a
future comparison meaningless exactly where it is most interesting.

### Module layout

```
jarvis/reasoning_distillation/
    config.py          every threshold, versioned (§14)
    models.py          provenance, canonical conversation, distilled schema (§6, §7, §11)
    storage.py         the local gitignored corpus root, and the two write gates (§4)
    privacy.py         explicit export status + auditable findings + meta-noise (§4, §13)
    adapters/          base.py, json_export.py, transcript.py (§5)
    normalization.py   source → canonical, identity attached (§6, §7)
    dedupe.py          exact + near duplicates → problem families (§8)
    splitting.py       family-level partitions, frozen holdout (§9)
    holdout.py         the firewall: HoldoutGuard + seven checks (§17)
    operators.py       the closed 26-operator taxonomy (§1)
    distiller.py       subtractive extraction + the bounded loop (§11, §12, §19)
    critic.py          ten checks for unsupported claims (§12)
    verifier.py        per-field source attribution (§12)
    quality.py         fifteen dimensions, no aggregate (§14)
    skills.py          multi-family evidence required (§15)
    benchmark.py       33 cases, 16 categories, runs nothing (§16)
    manifests.py       the build as a comparable value (§20)
    pipeline.py        the stages, in the only safe order
    cli.py             §21's stages as subcommands
```

---

## 5. The private corpus boundary (§4)

Raw historical conversations stay **local and outside Git**. The default root is
`~/.local/reasoning_corpus` — deliberately outside the repository, so no single `git add -A` can
commit it. An in-repo fallback (`.local/reasoning_corpus`) is gitignored.

`.gitignore` is a *claim*; `assert_root_is_gitignored` is a *measurement*. It runs
`git check-ignore` against the live root before anything is written, and treats an inconclusive
answer as a **refusal**, not permission.

### Two write gates, not one

| Class | Areas | Gate |
|---|---|---|
| Body-free | `reports/`, `manifests/`, `holdout/` | private-content scan **and** a 320-char string cap |
| Record | `distilled/`, `review/`, `rejected/`, `normalized/` | secret scan only; prose permitted |
| Raw | `raw/` | content-addressed bytes via `store_raw`; no JSON gate |

The three classes exhaustively partition the area list, asserted by
`test_every_area_has_a_declared_write_gate`. One gate for everything was measured wrong in each
direction: applied to a record area it refused an entire corpus because one rejected record quoted a
home path; applied to a report area it would let a manifest carry a paragraph of private
conversation.

### Body blindness is architectural

Every body-bearing type installs a body-free `__repr__`. Three ordinary language features compose
into a leak: a `@dataclass` renders every field, a container recurses into its elements, and
`repr` of a **bound method** interpolates `repr(__self__)` — so displaying `conversation.digest`
*without calling it* would otherwise render every turn. There is no `__repr__` on a method object to
override, so the container's own repr must already be safe.

### The measured secret leak

Extraction is subtractive: every populated field is a span lifted **verbatim** from a source turn.
A conversation containing an API key therefore produced a record containing that API key, and
`persist` refused to write the corpus as a result.

The refusal was correct and insufficient — a credential must not be *copied into a second file* at
all. Secret-bearing turns are now excluded from span lifting at the source; the turn itself is
preserved (§13), and the privacy finding still travels into the record, which stays honestly
`EXPORT_BLOCKED` with `secret_bearing_turns_excluded_from_lifting:N` recorded.

### Explicit export status

`EXPORT_UNKNOWN` is the default and it **blocks**. A clean scan of `INTERNAL` material yields
`EXPORT_BLOCKED`, not `EXPORT_SAFE`: absence of evidence of a secret is not a declaration that
material is shareable. `EXPORT_SAFE` requires a caller to declare `SYNTHETIC` or `LAB_FIXTURE`
explicitly.

### Auditable findings — the S4G lesson

S4G recorded that *the secret detector is unauditable*: a regex gate that can say only "there is a
secret in here". A reviewer handed that must either paste the private body somewhere or take it on
faith, and both defeat the control. Every finding here carries a **body-free diagnostic** — the
category, the count, the character span, and for a runtime-scanner hit its own category name — and
never the matched text. There is no `preview` returning real characters; the name is claimed and
made safe so nobody implements a helpful version.

---

## 6. Provenance and idempotence (§6)

Identity is `(source_file_hash, conversation_id, turn_index, raw_segment_hash)`. The **filename is
not part of identity at all** — only its extension influences adapter selection. Consequences:

- the same bytes under a different name produce an identical conversation, turn ids and digest
- two identical turns in *different* conversations get different `unit_id`s (a segment hash alone
  would merge them, and a merged unit can cross a split)
- `source_path_hint` is a basename; a full path is refused, so no derived record carries the
  operator's directory layout
- a declared conversation id is honoured; a derived one is prefixed `derived-` so the two cases stay
  distinguishable forever — a declared id is evidence about provenance and an invented one is not

Absent metadata is `""` = UNKNOWN. A model name is never guessed from prose style or filename, and a
file mtime is never recorded as a conversation date.

---

## 7. Ingestion (§5)

Only formats whose structure is decidable were implemented:

| Adapter | Why |
|---|---|
| `json_export` | a contract this repository **publishes and validates**, so nothing is guessed |
| `transcript` | role-prefixed `.txt`/`.md`, with a **closed** label table |

Deliberately absent, each with the reason and what would be needed: `docx`, `html`, `pdf`. A caller
handing over a `.docx` gets `AdapterNotImplemented` naming the gap and `CORPUS_INPUT_REQUIRED`.

The transcript parser is conservative in one chosen direction: a line that looks like a label but is
not in the table does **not** start a new turn — it is reported with its line number. An invisible
truncation (an answer split in half, its first half distilled as the whole decision) is worse than a
visible merge.

`<thinking>` / `<reasoning>` / `<scratchpad>` blocks are read as `ASSISTANT_REASONING`. Note the
deliberate asymmetry with `core.redaction_policy.strip_hidden_reasoning`, which *removes* them: the
runtime strips hidden reasoning because it must never **emit** it; M67A ingests it because it is
**studying** it. Nothing downstream may render such a segment.

### The input contract

```json
{
  "schema": "m67a.conversation.1",
  "conversation_id": "<stable id>",
  "source_model": "claude-3-opus",
  "source_date": "2024-05-01",
  "turns": [
    {"role": "user_request",        "text": "..."},
    {"role": "assistant_reasoning", "text": "..."},
    {"role": "tool_request",        "text": "..."},
    {"role": "tool_result",         "text": "..."},
    {"role": "assistant_answer",    "text": "..."}
  ]
}
```

`source_model` / `source_date` are optional; **absent means UNKNOWN and is never inferred**. An
unknown `role` becomes `UNKNOWN` and flags the conversation for human review rather than losing the
file. An unknown **top-level** key is refused — a mistyped metadata key is indistinguishable from
metadata that had no effect.

There is deliberately **no** `quality`, `success`, `correct`, `rating` or `score` field: a converter
must not be able to assert that a historical answer was good.

Roles: `system_context`, `user_request`, `assistant_reasoning`, `assistant_answer`, `tool_request`,
`tool_result`, `assistant_revision`, `follow_up`, `unknown`.

---

## 8. Deduplication and families (§8)

The grouping basis is the **problem** — `USER_REQUEST` turns only, joined by `U+001E`. Grouping on
the *answer* would merge unrelated problems answered similarly and separate two attempts at one
problem answered differently, which is exactly backwards: two attempts at one problem are precisely
what must not straddle the boundary.

Three transitive link types: identical conversation digest, identical normalized problem key, and
near-duplicate at or above the block threshold (0.80, inherited from
`training_gym.datasets.similarity`; warn at 0.60). A union-find with a lexicographically-smallest
representative makes the partition independent of input order.

A conversation with no `USER_REQUEST` turn cannot be grouped on its problem. It becomes a
**singleton** with `grouping_basis_absent=True` and is placed in DEVELOPMENT unconditionally — an
uncomparable family cannot be shown *not* to duplicate a holdout family. Signatures with an empty
problem statement are excluded from exact linking entirely; otherwise every ungroupable conversation
would share the digest of the empty string and merge into one family.

A search that hit the comparison ceiling reports `search_complete: false`. It never reports "no
duplicates found": a search that stopped early found nothing the way one that never ran found
nothing.

---

## 9. Splitting and the frozen holdout (§9)

Ratios are **0.70 / 0.15 / 0.15**, not 80/10/10, and the binding constraint is **families**, not
examples: 400 records from 30 families give a 10% holdout of three families, which cannot
distinguish a real improvement from which three problems were held out.

| Guard | Default |
|---|---|
| `min_families_for_holdout` | 24 — below this **no holdout is carved at all** |
| `min_families_per_partition` | 8 |
| `ratio_tolerance` | 0.12 (indivisibility only) |

An undersized corpus produces an empty frozen holdout, `defensible=False`, and an explicit
shortfall. `freeze()` **refuses** an empty or undersized holdout: freezing nothing would record that
a holdout exists when none does.

Placement is `sha256(algorithm_version, seed, family_id)` → sort → cut at the ratio boundaries.
`random.shuffle` is not used: its algorithm is not a cross-version guarantee, and a shuffle
re-permutes everything, so importing one family would silently move material **out of a frozen
holdout** while every report still said FROZEN. The hash-sort-cut gives determinism plus stability
under growth — adding a family moves at most the families adjacent to a cut.

### Growth

`verify_frozen` refuses a plan that changes a frozen holdout in either direction, or under a
different seed or algorithm version. `plan_respecting_frozen` is the legitimate growth path: frozen
families are **pinned**, everything else is placed among DEVELOPMENT and VALIDATION, and **no new
family ever enters the holdout**.

The cost is stated rather than hidden: the holdout's *share* shrinks as the corpus grows, and that
dilution is reported. The dilution margin is one family's worth of share floored at 2 points — **not**
`ratio_tolerance`, which exists to absorb indivisibility. Measured at 90 families against a 9-family
frozen set, the 0.12 tolerance silently absorbed a drift from 0.15 to 0.10 — a third of the holdout's
share, reported as no shortfall at all. A corpus that has outgrown its holdout needs a **new**
holdout, authored by a session that will not run it; that is an operator decision, not a rebalance.

---

## 10. The leakage firewall (§17)

Treated as a security boundary where **the adversary is us**. Nobody attacks a holdout; a developer
needs one more example to debug an extractor. So the design assumes every caller is honest and none
is careful.

`HoldoutGuard` is the only route to corpus content for distillation. It exposes **no method that
returns frozen-holdout content** — not a disabled one, not one behind a config key. The frozen
partition is reachable only as a set of family ids, which cannot reconstruct anything. It is an
*object that must be constructed and passed*, so `distil_corpus(conversations)` without it is a
`TypeError`, not an oversight.

Seven independently-reported checks:

1. `single_partition_per_conversation`
2. `family_never_straddles_partitions`
3. `near_duplicate_never_crosses_into_holdout`
4. `holdout_reachable_only_through_guard` — verified by **asking the guard**, not by reading intent
5. `every_item_has_a_family`
6. `export_safe_is_explicit`
7. `frozen_record_matches_plan`

**A missing check makes the audit unclean even with zero failures**, and `UNAVAILABLE` is a failure
state. When no frozen record exists, check 7 is `UNAVAILABLE` rather than `PASS`: "nothing has been
frozen yet" must not let an unfrozen corpus read as a protected one. A single boolean
"leakage: clean" is the unauditable shape this repository recorded as a defect.

The pipeline **refuses to distil** through a real leak. The absence of a frozen record alone is *not*
a leak — it is the honest state of a corpus too small to freeze — and the two are distinguished
rather than conflated.

---

## 11. Fact is not procedure (§10)

`FactualState`: `SOURCE_ONLY` (default), `VERIFIED`, `STALE`, `CONTRADICTED`, `UNKNOWN`,
`NOT_APPLICABLE`. Only `VERIFIED` is usable as truth, and **no extraction path can produce it** —
M67A verifies no external facts, and the critic raises BLOCKING if a record claims otherwise.

`FactualState.invalidates_procedure` returns **False for every member**. That is §10's rule made
executable: *a stale fact does not automatically invalidate a useful reasoning procedure.*

`FactProcedureLink` is what makes the distinction operational. Two cases the pipeline must tell
apart:

- a trace says "Python 3.9 is current" then correctly reasons *"a version claim is
  freshness-sensitive, so I must check rather than assert"* — the fact is stale, the procedure is
  excellent, `procedure_depends_on_claim=False`
- a trace says "this API has no rate limit" then builds a plan justified only by that claim — the
  procedure inherits the claim's status, `procedure_depends_on_claim=True`

When the link cannot be decided from the source it is `None` (undecided) and routes to human review.
Guessing would silently import the second case as the first. The deterministic extractor **never**
decides it.

---

## 12. Bounded recursive distillation (§12)

```
SOURCE → EXTRACT → CRITIQUE → REPAIR → VERIFY AGAINST SOURCE → ACCEPT / REVIEW / REJECT
```

**Extraction is subtractive.** Every populated field is a span lifted from a source turn; nothing is
composed. This costs recall — a constraint spread across two sentences is missed — and buys
attributability: the field *is* a turn's own words, so the verifier can name its witness. A
generative extractor writes a field that reads better than the source and cannot be attributed to
any single turn.

**Repair is subtractive too, and this is load-bearing.** The permitted actions are
`drop_unsupported_element`, `clear_unsupported_field`, `downgrade_verification_status`,
`reset_tool_execution`, `drop_unevidenced_operator`, `clear_invented_decision`. There is no ADD
action: if a repair pass could add a field, the loop would **launder a fabrication** — the critic says
"this constraint is unsupported", the repairer writes a better-supported-looking one, the critic
passes it, and an invented field enters the corpus with a clean audit trail. Findings are applied in
descending index order so an earlier drop cannot renumber a later one.

**Termination**, depth cap 3 by default (§12 caps it at 3; config validation refuses 4):

1. `no_material_findings` — the intended exit
2. `repair_converged_no_change` — byte-identical artifact, detected via `artifact_hash`
3. `depth_cap_reached` — and the record is then `NEEDS_HUMAN_REVIEW` / `RECURSION_EXHAUSTED`,
   **never ACCEPT**. Letting the cap act as an acceptance would make the bound a rubber stamp

Condition 2 matters more than it looks: without it, a finding the repairer cannot act on would burn
every pass, and the depth cap would become the normal exit rather than the exceptional one.

**The recursion is auditable.** Each pass persists artifact hash, parent hash, stage, critic
findings, repair actions and verification result. `depth` and `parent_artifact_hash` are *inside* the
content address, so two passes converging on identical content at different depths remain distinct
events. `chain_intact` proves there is no gap — a trace with a broken parent link cannot show what a
record was repaired **from**, which is the only thing that makes a repair reviewable.

### The critic

Ten deterministic checks, three severities (`BLOCKING`, `MATERIAL`, `ADVISORY`; only the first two
are material, and that is configuration). A check that **raises aborts the critique** rather than
being skipped: a critique missing a check reports fewer findings and reads as a cleaner record.

The load-bearing check is `unsupported_text` — every free-text field's content words must appear in
the source above a 50% overlap floor. It is deliberately lexical and generous; the error direction is
chosen, since a false positive costs a field and a false negative imports a fabrication.

`contradictory_trace` was **added during this milestone's own testing**: `RejectReason.CONTRADICTORY_TRACE`
was in the closed reason set and nothing could emit it, so a trace asserting "the cache evicts on
write", then "the cache does not evict on write", scored ACCEPT. It uses a high overlap floor (0.70),
a minimal singular fold confined to this check, and it is suppressed when the turn carries a
self-correction — a trace that contradicts itself **on purpose** is a different thing, and §18 keeps
them as separate fixtures.

### The verifier

The critic asks *"is anything wrong?"*; the verifier asks *"is everything attributable?"* A record can
pass the critic and fail here — the critic's grounding check asks whether words appear **anywhere** in
the conversation, which a field assembled from the conversation's own vocabulary satisfies. The
verifier asks which **single turn** supports the field, which that fabrication cannot answer.

Attribution floor 0.60, higher than the critic's because one turn is a smaller vocabulary than a
whole conversation. Task fields are witnessed by the operator's turns, decision fields by the
assistant's — a decision field attributable only to a USER turn is left an **orphan** rather than
credited, because the user describing an approach is not the assistant choosing one. `PARTIAL` is
**not** a pass; an empty record is `NOT_APPLICABLE`, never `VERIFIED`.

### Model-assisted extraction (§19)

Every deterministic stage works offline — asserted by
`test_no_module_in_the_package_imports_a_model_or_network_library`. A provider is reachable only
through `DistillationProvider`, and two properties are structural: it cannot see the holdout
(`assert_no_holdout_content` runs first), and its output has no authority — suggestions applied only
where the deterministic critic and verifier confirm them, and its critique findings ADVISORY-capped.
A provider-assisted build sets `deterministic=False` and names what the structural digest does not
cover. Model-assisted output is never presented as bit-reproducible.

---

## 13. Meta-noise (§13)

Detected conservatively: a segment is marked only when **two or more distinct markers** fire, so one
"I should" inside a page of real reasoning is a sentence, not self-talk. The threshold is
configuration.

**Source is never destroyed.** Nothing deletes, truncates or rewrites a turn; the pipeline declines
to *distil from* a marked segment, which is a different act. `privacy.preserve_source` cannot be set
to `False` — config validation refuses it, so there is no configuration that deletes source.

Markers: system-instruction discussion, model-identity chatter, policy/platform chatter,
scaffolding self-talk, formatting deliberation, hidden-state speculation, tool-routing internals.

---

## 14. The quality gate (§14)

**Fifteen dimensions, no magic scalar.** S4G reported a mean quality gain of +0.1714 that was six
safety flips — an aggregate moving in the right direction while the thing it summarised moved in the
wrong one. `QualityScore.mean` exists for reporting and **nothing reads it**; `disposition()` does not
receive it. A scalar no decision depends on cannot quietly become the decision.

Dimensions: `task_understanding`, `constraint_retention`, `reasoning_relevance`, `premise_handling`,
`uncertainty_handling`, `tool_decision_quality`, `alternative_quality`, `verification_quality`,
`self_correction_quality`, `factual_reliability`, `scope_adherence`, `answer_alignment`, `efficiency`,
`meta_noise`, `provenance_quality`.

Required: `task_understanding`, `constraint_retention`, `provenance_quality`, `scope_adherence`. At
most 2 soft-floor misses. All floors are configuration.

`provenance_quality` has a floor of **1.0** because it is binary in substance: either every field
traces to source or one does not. A floor of 0.9 would mean "nine of ten fields are attributable,
accept it" — and the tenth is precisely the one nobody can check.

Dispositions: `ACCEPT`, `NEEDS_HUMAN_REVIEW`, `REJECT`. **Every non-ACCEPT outcome carries a reason
code**, enforced by `QualityVerdict.validated()`: a rejection nobody can explain is a rejection nobody
can appeal. `REJECT` is reserved for records that are *wrong*; anything a machine merely cannot decide
— an undecided fact/procedure link, a surviving disagreement, an exhausted recursion, a contradiction
— routes to **review**, because discarding those would throw away the records most worth a human's
attention.

Reason codes: `DUPLICATE`, `LOW_INFORMATION`, `INSUFFICIENT_CONTEXT`, `MALFORMED_EXPORT`,
`META_POLICY_NOISE`, `UNSUPPORTED_DECISION`, `FACT_REASONING_ENTANGLED`, `PRIVACY_RISK`,
`CONTRADICTORY_TRACE`, `VERIFICATION_MISSING`, `UNKNOWN_SOURCE`, `CRITIC_DISAGREEMENT`,
`RECURSION_EXHAUSTED`, `QUALITY_FLOOR`.

> **Calibration: NOT CALIBRATED.** These scores derive from observable record structure and are not
> validated against human judgement. They filter for trustworthiness during corpus construction; they
> do **not** measure whether the historical reasoning was good. S4H recorded that a synthetic detector
> rate is not calibration, and the same caution applies. No claim in §1 rests on these numbers.

---

## 15. The reasoning skill library (§15)

A distilled record is an *observation*; a skill is a *claim about what generalises*. Three
independent support thresholds, because any one alone is gameable by a corpus that repeats itself:

| Threshold | Default | What it closes |
|---|---|---|
| `min_supporting_examples` | 3 | raw count — defeated by three near-copies |
| `min_supporting_families` | 2 | **the near-copy hole**: duplicates share a family by construction |
| `min_supporting_source_files` | 2 | one export is one witness → `PROVISIONAL` |

Statuses: `SUPPORTED`, `PROVISIONAL`, `INSUFFICIENT_EVIDENCE`. There is no member meaning "probably
generalises". A skill missing a threshold is **emitted with its actual counts**, not silently dropped:
"we saw this twice and need one more" is useful and a missing file is not. Only `ACCEPT` records
contribute support.

Skill **content** — trigger conditions, procedure, failure modes, stop conditions — is *authored in
this repository and held to*. The corpus supplies only the EVIDENCE that an operator recurs.
Synthesising the procedure from historical prose would give a skill file a model's wording and
nobody's authority. 10 of 26 operators have authored content; the gap is published by
`skills.coverage()` rather than hidden.

Skill files carry example **hashes** and never a raw trace, and the layer is separate from any future
learned weights — nothing here produces a training example.

---

## 16. Benchmark foundation (§16)

**33 cases, 16 categories, every category with at least one adversarial case** (one where the
tempting answer is wrong). A set of only easy cases reports a high score and measures nothing.

Metrics are behavioural, in §16's own terms — not "sounds intelligent" but *"correctly detects that
current external information is required before making a freshness-sensitive claim"*. Each case
declares an input and an expected **decision** on named dimensions, scored by a deterministic
comparator. Consequence worth stating: there is **no LLM judge**, so no judge to drift. Limit worth
stating: the harness can only measure what is expressible as a decision on a named dimension.

Cases are **synthetic and authored here**, never drawn from the corpus — a benchmark built from the
corpus would measure memorisation, and one built from the frozen holdout would spend it. This also
makes the harness committable, which no part of the corpus is.

`BenchmarkHarness.__init__` takes only `cases`. There is no provider, model, adapter, corpus or guard
parameter — nothing to pass it that would let it measure JARVIS. That is how §2's freeze is enforced
here rather than promised. An unanswered case is counted **apart** from a wrong one, so an arm cannot
improve its score by declining the hard cases.

Categories: premise correction, constraint retention, context priority, tool-needed vs not, tool
selection, freshness detection, uncertainty calibration, decomposition, alternative comparison,
verification behaviour, failure recovery, self-correction, hallucination avoidance, scope adherence,
stop-condition correctness, over-deliberation avoidance.

---

## 17. Manifests and reproducibility (§20)

A manifest records what a build **was**, as a value, so two builds are compared by comparing two
objects. Two layers, and the split is the design:

- `structural_inputs()` — versions, config, seed, source hashes, counts, artifact digests. Two builds
  with equal structural inputs and results are the same build.
- `environment` — timestamp, platform, interpreter. Recorded, **excluded** from the digest, and
  labelled as excluded. Including them would make every build unique and the reproducibility claim
  untestable.

Stage versions are collected by **calling** each module's `versions()`. A hand-maintained list would
go stale the first time a stage is bumped, and the manifest would confidently record the wrong
version — worse than recording none.

`build_id` is deterministic (sources + seed + pipeline version), not time-based: a timestamped id
would differ before the contents were even examined. `CorpusCounts.validated()` refuses counts that
do not reconcile — a record with no disposition was dropped without a reason code. The corpus **root
path is excluded**: it names the operator's home directory, and a manifest is meant to travel.

Verified: two runs over the same fixtures produce an identical `structural_digest`; a seed change
surfaces as `differing_keys: ["config", "split_seed"]`.

---

## 18. CLI (§21)

```
python -m reasoning_distillation.cli <stage> [options]
```

Stages: `contract`, `ingest`, `normalize`, `dedupe`, `split`, `distill`, `validate`, `review`,
`build-skills`, `report`, `benchmark-status`.

Options: `--input` (repeatable), `--recursive`, `--corpus`, `--config`, `--seed`, `--limit`,
`--sensitivity`, `--json`, `--no-llm`, `--dry-run`, `--write`, `--no-freeze`, `--generation`.

`--recursive` is **off by default**: §5 forbids recursively scanning a home directory, so a mistyped
path cannot sweep one into the pipeline. `--write` is required for any write — nothing is persisted
without it. `--no-llm` is accepted and **reported as a no-op**, because no provider is wired; a future
milestone must add `--llm` as the opt-in, so model assistance is something a run asks for rather than
something it gets by default.

Exit codes: `0` clean · `1` completed but not clean · `2` could not run · `3` `CORPUS_INPUT_REQUIRED`.
`1` and `2` are separate because "we measured and it failed" and "we could not measure" are different
facts.

One implementation backs `normalize`/`dedupe`/`split`/`distill`/`validate`: they are one ordered
pipeline, and five entrypoints each re-deriving a prefix would be five chances for the order to
differ.

---

## 19. Test coverage

| Suite | Tests | Covers |
|---|---|---|
| `test_reasoning_distillation_m67a.py` | 83 | §18 synthetic corpus, adapters, provenance, dedupe, splitting, operators, extraction, quality, skills, benchmark, manifests, CLI |
| `test_reasoning_privacy_m67a.py` | 39 | §4 explicit status, auditable findings, the secret-lifting leak, both write gates, gitignore verification, body blindness, §13 |
| `test_reasoning_leakage_m67a.py` | 26 | §17's seven checks, the guard's surface, frozen-holdout immutability in both directions, growth |
| `test_reasoning_recursion_m67a.py` | 28 | §12 termination, the cap not being acceptance, chain integrity, repair being subtractive |
| `test_reasoning_absent_controls_m67a.py` | 28 | §22 absent controls |
| **Total** | **204** | |

### Absent-control coverage (§22)

M66B ran 131 mutations with 0 survivors, and §22 names the limit: **mutation testing only tests
controls that already exist.** These tests mutate nothing; each asks *"what required control could be
completely missing?"*

- every source artifact has provenance
- every conversation belongs to exactly one family; every family to exactly one partition
- every ACCEPT item has a verified source link
- every private/export decision is explicit
- every recursive pass has a bounded, intact parent chain
- no skill can be supported by one family
- **no function taking a conversation list can omit the guard** — checked by *signature*, so no new
  caller can be written that bypasses the firewall
- **every reason code has a producer** — the generalisation of the `CONTRADICTORY_TRACE` finding
- every reason code is classified as reject-or-review (one in neither would silently ACCEPT)
- every quality dimension has a floor and is actually produced by the scorer
- every operator has detection markers (one without can be stored and never found)
- every stage publishes a version, and the manifest collects it
- every leakage check is declared expected
- every gating enum has an explicit unknown/blocking member, and no default is the permissive one
- every area any module writes to is a declared area
- no module imports a model or network library
- **only `storage.py` writes files**, so the gitignore check cannot be bypassed
- every body-bearing dataclass overrides `__repr__`

---

## 20. Known limitations

1. **No real corpus has been ingested.** Every number above is from synthetic fixtures. Real exports
   will surface adapter gaps that no amount of authored fixture predicts — that is why §5 forbids
   building adapters blind.
2. **Extraction is lexical and English/Spanish-biased.** Marker tables drive operator detection,
   constraint lifting and meta-noise. A trace reasoning well in unusual phrasing scores as having
   skipped the step. Detection asserts presence only, never absence, which bounds the damage but does
   not remove it.
3. **Quality scores are not calibrated.** See §14.
4. **The contradiction check is lexical** — high overlap plus opposite polarity. It will miss a
   semantic contradiction expressed in different words, and it is suppressed by any self-correction
   marker in the same turn.
5. **`FactProcedureLink` is never decided automatically.** Every stale or contradicted claim routes to
   human review. This is correct and it means a corpus with many stale facts has a large review queue.
6. **Only 10 of 26 operators have authored skill content.** The gap is published, not hidden.
7. **10 of 26 operators have no JARVIS decision-field counterpart.** A future comparison cannot cover
   them without new architecture.
8. **The holdout does not grow.** Pinning is the only safe growth path, so a growing corpus dilutes
   the holdout's share; a new holdout is an operator decision.
9. **The benchmark harness has never been executed against anything.** It is an instrument, not a
   measurement, and a harness nobody has run against a real system may have cases whose expected
   decision is arguable. The `rationale` field on every case exists so a disagreement is with a stated
   argument rather than a bare label.
10. **`git check-ignore` is the boundary for the in-repo fallback only.** The default root is outside
    the repository, where there is no Git to ask; protection there rests on the path being outside the
    worktree.
11. **No adapter exists for DOCX, HTML or PDF.** Recorded with reasons.

---

## 21. Corpus input required

No corpus path was supplied, so nothing real was ingested, no home directory was scanned, and no
historical conversation was read.

To proceed, supply either:

1. a path to sample files via `--input`, in `.txt` / `.md` (role-prefixed transcript) or `.json`
   (the §7 contract above); or
2. sample files in a format not yet supported, so an adapter can be built **against them** rather
   than against an assumption.

```
python -m reasoning_distillation.cli contract --json
```

prints the machine-readable contract, the implemented formats, the deliberately-absent ones with
their reasons, and the closed transcript label table.

**`FINAL_STATUS = M67A_INFRASTRUCTURE_READY_CORPUS_INPUT_REQUIRED`**

M67B (reasoning adaptation design) is **not** started and requires its own session and authority.
