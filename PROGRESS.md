# JARVIS V69 — M62 CURRENT CONTROL PLANE

> **This file is CURRENT STATE ONLY. It is not the event log.**
> Everything M62 has ever done through **S3N** is preserved byte-for-byte in the immutable
> archive below. This file describes only what is true *now*, and it is deliberately small
> enough to read at the start of every session.

| | |
|---|---|
| **Control plane** | V3 · schema `m62.control_plane.3` · state generation **17** |
| **Current state (machine-readable)** | `state/m62/current.json` |
| **Latest snapshot** | `state/m62/snapshots/0017-m63-s4b-candidate005-designed.json` |
| **Snapshot SHA256** | `da0656107d8727294dc86db866fb1117e1f605233be7eb72d852a3c2e7aa043a` |
| **Subject state commit** | `9e50a74c2f24cab905b9aaaac18e2970255db24e` (S4B preregistered candidate 005; nothing trained, evaluated or promoted) |
| **Receipts & records** | `state/m62/receipts/` (portable training/eval proof) · `state/m62/records/` (V3 content-addressed immutable blocks) |
| **Historical archive** | `jarvis/docs/m62/history/PROGRESS_THROUGH_S3N.md` |
| **Archive SHA256** | `e0914054da4dde4b785bbdabc45a40e0f8b590c2aa3612e9432c685c0c79c1bf` |
| **History index** | `jarvis/docs/m62/HISTORY_INDEX.md` |
| **Verify first** | `python jarvis/scripts/verify_m62_control_plane.py` |

---

## 0 — Verify before you act

```
git fetch origin --prune && git status -sb
python jarvis/scripts/verify_m62_control_plane.py     # expect: PASS / PROBLEMS: 0
```

The verifier is offline, read-only, deterministic and fail-closed. It loads no model,
tokenizer or weights, writes nothing, opens no socket. It does not trust this file: it
re-derives policy digests from the production classes, re-hashes the archive and snapshot, and
asks Git — not prose — about the branch, ancestry and `master`.

**If it fails, stop.** See §14.

---

## 1 — Checkpoint

| Field | Value |
|---|---|
| Repository | `alejandrobalsas26-sys/jarvis` (`origin`, HTTPS) |
| Branch | `jarvis-v69-m63-world-state` |
| Subject state commit | `9e50a74c2f24cab905b9aaaac18e2970255db24e` — the commit the current snapshot describes |
| Training source commits | candidate 003 `bac49c4a49194d84fbc7f61656662fdcd54799ca`; candidate 004 `80565d32795fb276df202f6bef46ed38bb2bb7c5`. **Deliberately different from the subject commit** |
| HEAD | a descendant of the subject commit; resolve with `git rev-parse HEAD` |
| Divergence from origin | `0  0` |
| `origin/master` | `3705114228edef2f665be349c5c4429b7b16777a` — **untouched by M62** |
| Merge / tag / release / version bump | **none** — `core/version.py` still declares `MILESTONE = 61`, deliberately |

**Every hash in this file is a current identity, not a restart target.** Start from current
HEAD; do not reset to an earlier M62 checkpoint.

**Control-plane commit vs subject-state commit.** The snapshot describes the repository at the
*subject* commit; a milestone's phase-B commit adds only control-plane and documentation files
on top. That is why the two differ, and why the verifier requires HEAD to *descend* from the
subject rather than equal it.

---

## 2 — Current milestone status

| | |
|---|---|
| Milestone | **V69 M62 — Training Gym** |
| Last state-bearing milestone | **S4B** — candidate 005 `DESIGNED_UNTRAINED` under human operator ruling **S4B-001**; nothing trained, evaluated or promoted |
| Last milestone | **S4B** — `V69_M63_S4B_CANDIDATE005_SINGLE_AXIS_DESIGN.md`. Design, ruling and a rebuilt training runtime, all body-free. **0** weight loads, generations, eval attempts, holdout spends |
| Phase | **PREREGISTERED, UNTRAINED.** Candidates 001–003 `EVALUATED_NOT_ELIGIBLE`; **candidate 004 stays `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` under its HOLD, not promoted**; candidate 005 `DESIGNED_UNTRAINED`; `eval-v6` `USED_IMMUTABLE`; **no fresh eligibility corpus remains** |
| Live training since S3N | **two runs** — candidates 003 and 004, 40/40 optimizer steps each, both `TRAIN` capabilities spent. **No retry is authorised** |
| Live evaluation since S3N | **two runs** — S3Q (003 × `eval-v4`) and S3Y (004 × `eval-v6`). One plan, one holdout commit and one terminal event each. Both corpora are `USED_IMMUTABLE`; **no rerun of either is possible** |
| S3Q.0.2 | evidence only — **0 weights loaded, 0 generations, 0 new evaluation attempts, no plan consumed, no figure moved** |
| Next | **One single-use human `TRAIN:<plan-hash>` for candidate 005**, supplied out of band. It authorises **exactly one** run and nothing else — no evaluation, no `eval-v7`, no promotion, no candidate 006. **No `TRAIN`, `EVAL` or promotion authority exists in this repository** |

**What M62 is.** The Training Gym: an offline-first, human-gated pipeline that grades
defensive episodes, builds immutable leakage-checked datasets, runs a bounded LoRA fine-tune
under a single-use token, and runs a paired baseline-versus-adapter evaluation over a held-out
corpus, ending in a *non-effectful* proposal. **Nothing has been promoted.**

**Candidates 001–003 are `EVALUATED_NOT_ELIGIBLE`, and each failed differently** — 001
over-refused; 002 lost every required refusal, failing three of nine security vetoes; **003
clears security outright** but is blocked by **one deterministic quality gate**, 9/9 → 8/9.
**004 cleared every deterministic gate** and is held. **No midpoint is demonstrated**, and no
ablation may run against a spent holdout. Detail: §4. **Nothing has been promoted.**

---

## 3 — Base model

| | |
|---|---|
| Model / tokenizer | `Qwen/Qwen3-0.6B` |
| Revision | `c1899de289a04d12100db370d81485cdf75e47ca` (`immutable_commit`) |
| Chat template digest | `a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8` |
| Execution | offline, reviewed local cache, `local_files_only=true`, `trust_remote_code=false` |
| Training precision | **FP32 / CPU** (S3H, S3K) |
| Tracked authority | `jarvis/scripts/build_quality_training_config.py` |

The S3E.2 evaluation load was observed at **bfloat16** because the backend did not force FP32;
that is history and is not rewritten. `tokenizer_chat_template_hash` digests the template
**source**, not the call — read `chat_render_policy_hash` for the call (D37).

---

## 4 — Candidates

| Candidate | State | Adapter SHA256 | Trained on | Measured on | Deep authority |
|---|---|---|---|---|---|
| `qwen3-06b-lora-quality-live-001` | `EVALUATED_NOT_ELIGIBLE` | `43213035…e22ac858` | `train v1` | `eval v2` | `V69_M62_S3I_LIVE_QUALITY_HELDOUT_EVALUATION.md` |
| `qwen3-06b-lora-quality-live-002` | `EVALUATED_NOT_ELIGIBLE` | `319c2524…f9665409` | `train v2` | `eval v3` | `V69_M62_S3L_SECOND_QUALITY_HELDOUT_EVALUATION.md` |
| `qwen3-06b-lora-quality-live-003` | **`EVALUATED_NOT_ELIGIBLE`** | `6ccd8fdc…c76ce4ea6` | `train v2` | **`eval v4`** | `V69_M62_S3Q_CANDIDATE003_LIVE_HELDOUT_EVALUATION.md` |
| `qwen3-06b-lora-quality-live-004` | **`EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW`** | `a105e01c…a1c3ecc67` | `train v2` | **`eval v6`** | `V69_M62_S3Y_CANDIDATE004_LIVE_HELDOUT_EVALUATION.md` |
| `qwen3-06b-lora-quality-live-005` | **`DESIGNED_UNTRAINED`** | *none — no weights exist* | `train v2` | *never — no exam exists* | `V69_M63_S4B_CANDIDATE005_SINGLE_AXIS_DESIGN.md` |

**Candidate 003 is MEASURED and NOT ELIGIBLE.** S3O designed it moving **exactly one** primary
axis — training render reasoning policy `MODEL_DEFAULT` → `DISABLED`; S3P spent the one
authorised `TRAIN` capability on it; S3Q spent `eval-v4` on it. It **clears security
outright** (blockers 0, new regressions 0) and is blocked by one deterministic quality gate,
schema validity 9/9 → **8/9**; its mean delta does **not** exclude a regression. Full figures:
`…S3Q_CANDIDATE003_LIVE_HELDOUT_EVALUATION.md`. **`EVALUATED_NOT_ELIGIBLE` is terminal:** it
improved several measurements while failing the frozen gate, and it is **NOT approved, NOT
promoted and NOT production-ready.**

**Candidate 004 is MEASURED, ELIGIBLE and HELD.** S3U built it from candidate 003's
configuration with **one dial moved** — `learning_rate` 1e-4 → 5e-5 — on an explicit human
ruling superseding only the learning-rate clause of one generation-8 entry, for that
candidate only. S3V spent **one** plan-bound single-use `TRAIN` authority: 40/40 optimizer
steps, 2/2 epochs, 0 truncations, one verified 392-tensor adapter. S3Y then spent `eval-v6`
on it **once**; every preregistered deterministic gate passed, security regressions were
**0**, and the operator decided **HOLD**. It is **not promoted** and **no retry exists**.

**Candidate 005 is DESIGNED and UNTRAINED.** S4B built it from candidate 004's configuration
with **one dial moved** — `learning_rate` 5e-5 → 2.5e-5 — on a second explicit human ruling,
**S4B-001** (§12). Rank 16, alpha 32, `alpha/r` 2.0, dropout 0.05, 2 epochs, 40 steps, seed
42, `ATTENTION_AND_MLP`, `train v2` and reasoning `DISABLED` are inherited **by construction,
not re-typed**; the measured semantic diff is exactly `{learning_rate}`. **No weights, run,
receipt, exam or authority exist for it.**

**A training loss decides nothing.** Train and validation losses are diagnostic, **not**
eligibility evidence, and may not be used to tune or select. Candidate 005's eligibility is
**UNKNOWN and unmeasurable here**: `eval-v4` and `eval-v6` are spent, `eval-v5` is
`FROZEN_UNUSED`, never model-spent and **retired from eligibility use** (§5, D44), and **no
`eval-v7` exists**. `TRAINING_ROOT_CAUSE_CONFIDENCE` stays **NOT_ESTABLISHED**.

**Neither claim is this table's to make.** `check_training_receipt` and
`check_evaluation_receipt` re-derive both from the tracked, root-independent receipts in
`state/m62/receipts/`, and refuse a snapshot that agrees with a verifier constant while a
receipt is absent or disagrees; the `EVALUATED_*` verdict is re-derived by the **production**
decision function, never read. `config_hash` and `plan_hash` are root-bound: re-derive, never
paste. **No retry exists** for either.

**Closed candidate-state vocabulary.** `NOT_CREATED` · `DESIGNED_UNTRAINED` ·
`TRAINED_UNEVALUATED` · `EVALUATED_NOT_ELIGIBLE` · `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` ·
`EVALUATED_NEEDS_MORE_EVIDENCE` · `EVALUATED_QUARANTINED` · `PROMOTED`. Anything else is
refused, and the verifier rejects every transition absent from its allowed table — including
`NOT_CREATED → PROMOTED`, `TRAINED_UNEVALUATED → PROMOTED` and
`EVALUATED_NOT_ELIGIBLE → PROMOTED`. `EVALUATED_NOT_ELIGIBLE` is **terminal**.
**`PROMOTED` is refused outright in a snapshot:** no promotion mechanism exists here —
`ModelCandidateProposal` is non-effectful by construction — so no artefact could witness it.

---

## 5 — Datasets

| Dataset | Role | State | Manifest | Parent | Pack | Spent by |
|---|---|---|---|---|---|---|
| `m62-defensive-eval v1` | holdout | `USED_IMMUTABLE` | `0970600c…` | genesis | `d714d89b…` | S3E.2 |
| `m62-defensive-eval v2` | holdout | `USED_IMMUTABLE` | `82b60bfd…` | `0970600c…` | `3744a22e…` | S3I LIVE |
| `m62-defensive-eval v3` | holdout | `USED_IMMUTABLE` | `7c948236…` | `82b60bfd…` | `28d2f7d0…` | S3L |
| `m62-defensive-eval v4` | holdout | `USED_IMMUTABLE` | `8c6871b0…` | `7c948236…` | `95b4e2f6…` | S3Q, candidate 003 |
| **`m62-defensive-eval v5`** | holdout | **`FROZEN_UNUSED`** · **eligibility RETIRED** | `e852f462…` | `8c6871b0…` | `287a9fb6…` | **nothing — never model-spent** |
| **`m62-defensive-eval v6`** | holdout | **`USED_IMMUTABLE`** | `413e6757…` | `e852f462…` | `41579381…` | **S3Y, candidate 004** |
| `m62-defensive-quality-train v1` | training | `USED_IMMUTABLE` | `9bbac2f0…` | genesis | — | S3H |
| `m62-defensive-quality-train v2` | training | `USED_IMMUTABLE` | `24ceb1e0…` | `9bbac2f0…` | — | S3K, S3P, and reused UNCHANGED by candidate 005 |

Full 64-character digests are in the snapshot records. Every holdout is 36 tasks, splits
12/12/12, families 12/9/9/6, classes 12/6/18 — the frozen contract, cell for cell.

**`FROZEN_UNUSED` vs `USED_IMMUTABLE` is a scientific property, not a label.**
`FROZEN_UNUSED` means **no model has ever read it**; `USED_IMMUTABLE` means it is spent and
its results are design input from then on (**D35**). The transition is **one-way**, the
verifier has no edge back, and a missing status is a **failure**, not a fresh corpus.
(`FROZEN_UNSEEN`, S3J/S3K, is the same state, archive-only.)

### `eval-v6` — SPENT ONCE on candidate 004 (S3X.1 froze it, S3Y spent it)

```
dataset_id / version    m62-defensive-eval / v6      status  USED_IMMUTABLE
spent_by                S3Y LIVE, candidate 004 (plan e2b591fe, report d708d721)
manifest                413e675711d51f5b98cb5a8ec7ff7fb0d8eb36b5e4c6dff790fb60f764f8fba6
parent                  e852f462…  (eval-v5, declared not discovered — D34)
```

**Set digests bind `v6` without reading a body.** Freshness was **measured** before the spend
— 2,564 comparisons against `v1`–`v5`, **0 overlaps, 0 WARN, 0 BLOCK** — and it was authored
**candidate-blind**. Sealed history: `…S3X1_EVAL_V6_FREEZE.md` ·
`…S3Y_CANDIDATE004_LIVE_HELDOUT_EVALUATION.md`. **No rerun or second look is possible.**

### `eval-v5` — frozen, never model-spent, RETIRED from eligibility use

```
dataset_id / version    m62-defensive-eval / v5      status  FROZEN_UNUSED   spent_by null
manifest                e852f4627d4fe631f58ee3d120d5d1a81c94480a1c0b84e590d2b08261043f4c
task / prompt / target  cda48cf5…  ·  239c6402…  ·  47dbb2a0…      set digests, body-free
```

**RETIRED FROM ELIGIBILITY USE at generation 12, and still `FROZEN_UNUSED` with `spent_by`
null.** Both halves are true and neither collapses into the other: **no model ever saw it** —
0 weight loads, 0 generations, 0 spend events, no receipt — while **one body reached an
orchestration session pre-authorisation** (**D44**), destroying the preregistered freshness.
**It may never be eligibility evidence for any candidate**, candidate 005 included. Its bodies
stay **unread**; `v6` declaring it an **ancestor** is lineage, not reuse. Freeze/retirement:
`…S3S_EVAL_V5_FREEZE.md` · `…S3X0_PRESPEND_HOLDOUT_FIREWALL_RECOVERY.md`.

**`eval-v4` is spent** (S3Q, candidate 003 — freeze in `…S3N_FRESH_EVAL_V4_FREEZE.md`,
results in `…S3Q_CANDIDATE003_LIVE_HELDOUT_EVALUATION.md`). **Under D35 it is development
evidence and may never decide eligibility again.** Its bodies stay unread.

---

## 5b — The evaluation ceremony

Qualified before `eval-v4` was spent (S3Q.0), evidence chain closed before it (S3Q.0.1),
executed once (S3Q), sealed after (S3Q.0.2). Deep authority, in that order:
`…S3Q0_…` · `…S3Q01_…` · `…S3Q_CANDIDATE003_LIVE_HELDOUT_EVALUATION.md` · `…S3Q02_SEAL_RECOVERY.md`;
`…S3Y_…` owns the candidate-004 measurement. They own the full body-free results, the
field-by-field receipt derivations and the S3Q figures.

**Four events, four different facts** (§8): `PLAN_CONSUMED` ·
`HOLDOUT_MODEL_FACING_COMMITTED` · `EVALUATION_COMPLETED` · `TERMINAL_LEDGER_RECORDED`. Each
live evaluation recorded **exactly one of each**, under **one** plan hash.

**PROSPECTIVE SPEND RULE** (§8). A holdout is `USED_IMMUTABLE` the moment the evaluator
**durably commits** the first held-out request to the model-facing boundary — after request
parity, immediately before the first `backend.generate`. Once written it is spent whatever
happens next, and **RERUN IS FORBIDDEN**: not for a crash, a failed artefact write, a lost
terminal line, or a receipt that will not build. `eval-v4` crossed 2026-08-18 and `eval-v6`
at S3Y. **`eval-v5` never crossed it**, which is why it is `FROZEN_UNUSED` and not spent.

**PORTABLE RECEIPTS.** `m62.eval_receipt.3` and `m62.train_receipt.1` (`scripts/build_m62_*_
receipt.py`) are deterministic, body-free, atomic, and the **only** things that may carry an
`EVALUATED_*` or `TRAINED_*` state out of a gitignored runtime tree. Eligibility is
**re-derived** by production `decide_eligibility`, never copied. (`.2`'s three refusals were
**D40 · D41 · D42**, §7.) **THE MEASUREMENT WITNESS** (`state/m62/witnesses/`) is **not a
receipt:** it grants no state, authorises no retry, promotes nothing, and establishes
**repository provenance, NOT execution attestation** — nothing is signed, no PKI implied.

**Body boundaries.** `ORCHESTRATOR_SEMANTIC_ACCESS` forbidden — **and enforced in memory, not
only on disk (D44)** · `BODY_OPAQUE_PROGRAMMATIC_ACCESS` permitted for reviewed
hashing/validation code · `MODEL_FACING_ACCESS` is the spend. `task-pack.jsonl` is
**`BODY_BEARING`** by design; every other artefact, ledger line, witness and receipt are
**`BODY_FREE`**.

---

## 6 — Policy identities

Re-derived by the verifier from the production classes on every run. They are not read from
this table.

```
gate_policy_hash         e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5
metric_policy_hash       e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a
generation_policy_hash   c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7
reasoning_policy         DISABLED
max_new_tokens           512
D38 read by any gate     NO
```

**One reconciliation, so it is not rediscovered as drift.** `eligibility_generation_policy()`
*alone* hashes to `1b4696d6…` (library defaults: `timeout_s` 120, `seed` 0, auto-safe device
and precision); `c6b0b682…` is the **configured** policy the sealed S3I and S3L config
documents declare (`timeout_s` 300, `seed` 11, `cpu`, `fp32`). Both are correct, they are
different objects, and the verifier requires them to differ. `config_hash` and `plan_hash`
bind `output_root_id`, runtime and hardware evidence: **root-dependent — re-derive on the
executing host, never paste a recorded value in.**

---

## 7 — Open defects and current limitations

Only what still binds operation. Closed historical defects (D1–D27) live in the archive and
are indexed in `jarvis/docs/m62/HISTORY_INDEX.md`.

| Defect | State | What it still binds |
|---|---|---|
| **D25** | FIXED | A persisted report serialised in `run_state: comparing` is the documented serialisation state, **not** an unfinished run. |
| **D28** | **OPEN** | No tool-call transport exists, so `tool_call_validity_rate` is **VACUOUS** on both arms and the six `tool_call_schema` tasks decide nothing. |
| **D29** | ACCEPTED LIMITATION | `looks_like_refusal` reads sixteen literal phrasings the JSON refusal targets do not contain. It bounds QG-1 and SV-5 **in both directions**, and travels into `v4` by design. |
| **D30** | FIXED | A plan may not report `is_executable` with an unverified model cache. |
| **D31** | FIXED | VALIDATION is wired and **diagnostic only**. Do not remove `eval_dataset` or the closing `trainer.evaluate()`. |
| **D32** | SUPERSEDED | By D34. |
| **D33** | **OPEN** | The declared generation timeout is **not enforced**, so `timeout_rate` is **VACUOUS**. A config must state `timeout_s` explicitly; the default is 120 s. |
| **D34** | FIXED | A dataset parent is **DECLARED**, never discovered from disk. Fails closed rather than degrading to genesis. |
| **D35** | OPERATOR RULING | A spent holdout becomes development evidence. **Each candidate needs a fresh holdout.** Not a contamination claim. |
| **D36** | FIXED | The identity redactor matches unless flanked by ASCII letters on both sides. Do not simplify it to a substring match; do not widen it to `\b`. |
| **D37** | FIXED | Training binds a reasoning policy and `chat_render_policy_hash` binds the *call*. **Exercised by one live run** (candidate 003, S3P, render `8619f96c…`). **Historical causality NOT_ESTABLISHED**; closing it is *not* predicted to restore 9/9, and nothing has measured whether it changed anything. |
| **D38** | FIXED (observability only) | Output-budget exhaustion is a body-free diagnostic beside the unchanged input-truncation metric. **No gate reads it and none may be added without a separate operator decision.** Reaching the ceiling is not failure. |
| **D39** | **OPEN** | Order-dependent test isolation between the S3G.2 validation-wiring file and the dataset-exports file. No recorded figure has ever been affected. **Not to be fixed as a rider.** |
| **D40** | FIXED at `.3` | A receipt may not model the paired outcome as an exhaustive three-way `wins/ties/losses` partition. Production classifies **four** comparable verdicts and `security_improvement` is **not** a win. |
| **D41** | FIXED at `.3` | An ASCII-only canonical-text rule refuses legitimate production decision text (`U+2212`). Close the encoding question by **defining** the encoding, never by discarding evidence. |
| **D42** | FIXED at `.3` | Deriving the evaluation source from HEAD at receipt-build time conflates the code that **measured** with the code that **built the receipt**, and makes truthful post-live sealing impossible. |
| **D43** | FIXED (observability only) | A JSON parse failure persisted one closed note code, so `EXTRA_DATA` — a whole document followed by more output — was indistinguishable in the evidence from a document that never closed. Fixed **prospectively** at S3T.0 with a closed body-free class plus location, and one repetition scalar. **No gate reads any of them and none may be added without a separate operator decision.** Nothing historical is backfilled. |
| **D44** | **FIXED · GATE** | A held-out body reached an orchestration session **before any authorisation existed**, through Python's representation machinery alone. The **persistence** firewall held — nothing reached a tracked file, receipt or ledger; the missing one was the **in-memory orchestration-display** firewall, including `repr` of a **bound method**, which interpolates `repr(__self__)`. `schemas.body_free_repr` renders identity and digests only, guarded **by type**. Representational only: **20 families of evaluation identity are byte-identical.** Mechanism and proof: `V69_M62_S3X0_…md`. |

### Limitations that travel into any successor run

- Gate thresholds are **uncalibrated** (`thresholds_are_calibrated: false`).
- Every holdout is **36 synthetic tasks, one author, one session, no independent review**;
  `tool_call_schema` has only 6. **Semantic leakage has never run** — all freshness and
  leakage evidence is exact or lexical, so a pure paraphrase would not be caught.
- **Nothing is head-to-head comparable.** 001 was measured on `v2` and 002 on `v3` with zero
  shared instances, and a candidate fitted under `DISABLED` is not comparable to either —
  binding the policy is itself an axis. What is comparable is each candidate against its
  **own** simultaneously-measured baseline under identical policy digests.
- **Candidate 003 was measured ONCE.** One host, one CPU, one seed, no repeat, no second
  host, no GPU, no dtype control arm. Its 12-row train-time validation is steering material,
  appears in no gate, and is **not** comparable with 001's or 002's numbers.
- **The interval does not exclude a regression.** Mean delta +0.044208, CI95
  [−0.022359, +0.129413] over 36 pairs; recorded as `regression_not_excluded`, **not** as an
  improvement. The blocking gate is one task moving on a 36-task holdout, which cannot be
  distinguished from noise — and **no ablation may be run against a spent holdout**.
- **The D37 axis is neither confirmed nor refuted.** One live training run and one live
  measurement; D37's historical causality remains `NOT_ESTABLISHED`.
- Measurement is **one host, CPU, one seed, one run per candidate** — no repeat, no ablation;
  `deterministic_reproduction_claimed` is `false`. Neither Kali runtime is claimed bytewise
  equivalent to the Windows runtime that produced 001's adapter.
- `openai` is a declared base dependency absent from the system interpreter; its absence
  alone fails 62 tests in three files. Environmental, reproduced at pristine HEAD.
  **Never reconcile test counts across interpreters by arithmetic.**
- **`m62.eval_receipt.3` has described exactly ONE real evaluation**, and `.2` still none.
  A synthetic qualification is evidence about the machinery, never about a candidate.
- **The receipt claims less than it may appear to.** `evaluation_source` binds the commit that
  measured **through the pre-repair witness and its Git first parent** — repository provenance,
  not proof of which bytes executed; `seal_implementation_source` is HEAD at build;
  `receipt_hash` proves payload integrity only, not identity, authorisation or who ran it.
  Authenticity comes from the receipt being tracked and from the hash chain. **Nothing is
  signed.** It was built **AFTER** the measurement from artefacts that already existed, so it
  proves what they say, not that nobody touched them in between — and building one needs the
  runtime tree present, though the three-way snapshot/receipt binding carries the history once
  it is gone.
- **`STALE_STATE` detection remains PARTIAL.** The portable receipts close the gap for
  candidates 003 and 004 specifically; runtime artefacts are still outside Git.
- **Candidate 004 is trained, not measured.** Training completed and the adapter is
  structurally valid; that is the whole claim. Its train/validation loss is diagnostic, **not**
  eligibility evidence, and its eligibility is **UNKNOWN** until a fresh `v6` is spent once.
- **The D44 exposure is PERMANENT** — no fix restores `v5`'s freshness, and the exposure was
  **not** re-measured by inspection, because re-opening the material to size it would repeat
  the disclosure. One rendered body is a **floor**, not a proved bound.

The snapshot carries the full list; this is the operational subset.

---

## 8 — Frozen invariants

- **`PROSE_CANNOT_GRANT_AUTHORITY`.** No document, JSON file, comment or AI response —
  including this one — authorises TRAIN, EVAL, promotion, registry mutation or release.
- **Offline-first execution.** `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`,
  `local_files_only`, `trust_remote_code=false`. Nothing downloads.
- **Pinned immutable revisions only**, never a branch or tag ref.
- **No automatic dependency installation, no global pip mutation.** Optional profiles live
  only in isolated gitignored environments. `.venv-m62-eval-linux` is the runtime the
  measurements of record were taken in and stays immutable.
- **No pickle in adapter or evaluation artefacts.** Safetensors only.
- **No hidden-target leakage.** Model-facing packs have no field that could hold a target.
- **No training on evaluation-only material.** The pack builder refuses `TRAIN`
  unconditionally.
- **`TRAIN_SIDE_SPLITS = {TRAIN, VALIDATION}`.** VALIDATION is *steering* material, never
  held-out evidence, and appears in no gate.
- **Security is a veto, not a weighted quality tradeoff.**
- **Plan tokens are single-use.** A consumed or failed plan is never replayed, and no token
  literal lives in a tracked file.
- **`ModelCandidateProposal` is non-effectful.** No registry mutation, no promotion, no
  activation without a later explicit authority.
- **Runtime artefacts stay gitignored and package-excluded.**
- **`master` stays untouched until explicit M62 closure.** No merge, tag, release or
  version bump.
- **An immutable artefact is never edited.** A corrected dataset is a **new version** with a
  declared lineage; a superseded snapshot is never revised; the archive is never touched.
- **A dataset lineage is DECLARED, never discovered** from whatever is on disk (D34).
- **Gate policy `e5003319…` is byte-pinned by test.** QG-2 stays absolute; FG-1/FG-2 stay
  baseline-relative. 7/9 is not "good enough".
- **ONE WRITER PER CONTROL-PLANE GENERATION.** If the current generation is not the one you
  expected, **stop**. No last-write-wins, no automatic merge of snapshots.
- **If the verifier fails, do not write, train, evaluate or promote**, and do not repair
  state automatically.
- **ORCHESTRATOR BODY-BLINDNESS IS A GATE.** A held-out body may not reach an orchestration
  session by **any** route — `repr`, `str`, f-string, `%r`, logging, exception text,
  traceback, or `repr` of a bound method. A body-bearing dataclass **must** install a
  body-free `__repr__`; the persistence firewall alone is **not** enough (**D44**).
- **`eval-v5` ELIGIBILITY USE IS RETIRED**, prospectively from generation 12. It stays
  `FROZEN_UNUSED`, `spent_by` null, because **no model ever saw it**; its preregistered
  body-blindness precondition failed pre-authorisation. **`FRESH_V6_REQUIRED`.**
- **An `EVALUATED_*` state is REDERIVED, never read.** The portable receipt carries the
  body-free gate, bootstrap, empirical-status and serialisation-state evidence the decision
  was made from, and the **production** `decide_eligibility` is asked what they conclude.
  **There is exactly one eligibility algorithm**, and a receipt that merely *states* a
  verdict evidences nothing.

---

## 9 — Authority observation

```
train / eval / promotion            NONE_OBSERVED_IN_REPOSITORY
control_plane_can_grant_authority   FALSE
```

**Two `TRAIN` capabilities have been created and consumed** — S3P's on candidate 003, S3V's
on candidate 004 — and one `EVAL` capability, S3Y's on `eval-v6`. All are spent. No reusable
capability exists and no replacement may be minted; **a spent single-use token is not an
authority anyone still holds.** No authority of any kind exists for candidate 005.

**This is an OBSERVATION, never a grant.** Plan tokens live outside the repository by
invariant, so it is *measured* as "no tracked file carries a token literal" — the verifier
scans on every run — and is **not** proof that none exists elsewhere. Absence of evidence is
not a clean measurement; that is the D38 lesson. TRAIN, EVAL, promotion, registry mutation and
release remain governed **exclusively** by the single-use plan-token mechanism plus an explicit
human operator decision, which no milestone since has moved, replaced or weakened.

### Operations requiring new explicit operator authorisation

| Operation | Why |
|---|---|
| Any live training | a fresh plan and a fresh single-use `TRAIN:` token |
| Any live evaluation | a fresh generation, a fresh plan and a fresh single-use `EVAL:` token |
| Freezing a fresh `eval-v7` | a new session that did not design the candidate, plus an explicit human decision; it evaluates nothing |
| Registry mutation, promotion, activation, role assignment, adapter merge | no authority here grants these |
| Merging M62, tagging, releasing, bumping `core/version.py` | M62 closure is an explicit operator decision |
| Touching the global environment, or any network or model-hub contact at run time | the no-global-mutation and offline-first invariants |

---

## 10 — Authoritative test baseline

```
invocation      pytest -k m62 --ignore=tests/test_live_brain_v61.py
run from        jarvis/   (repository system interpreter)
result          4648 passed · 20 skipped · 0 failed        [S4B]
```

S4B's own additions are **`-k m63`** and are counted separately: 90 candidate-005 design
tests, the S4B control-plane suite and 8 runtime-doctor regression tests. Its edits to the
sealed S3U suite moved **witnesses, not properties** — the notation refusal, the candidate
roster and the unknown-candidate id — each argued in `…S4B_CANDIDATE005_SINGLE_AXIS_DESIGN.md`;
**0** threshold, gate, grader, policy, receipt or transition weakenings. Counts are **one**
interpreter's; **never reconcile across interpreters.**

**Rescoped assertions are not regressions.** An assertion comparing a *sealed* milestone's
property against *live* state also asserts, silently, that no later generation exists — true
by coincidence until the next milestone writes one. Such tests are pinned to the generation
that recorded the property, and each rescoping is argued in its own milestone document.

**Known invocation-context artefact — do not rediscover it as a regression.** Running `pytest`
from the **repository root** instead of `jarvis/` fails **8** tests in
`…s3g2_validation_wiring.py`, which read production source by a path relative to the working
directory. S3N reproduced the same 8 pre-S3N. **It is distinct from D39**, which fails **4**
tests in the *dataset-exports* file. Do not conflate them or fix as a rider.

---

## 11 — READ FIRST — the bootstrap contract

A normal session reads **four things**, in this order:

```
LEVEL 0  VERIFY      python jarvis/scripts/verify_m62_control_plane.py
LEVEL 1  CURRENT     state/m62/current.json + the snapshot it points at + PROGRESS.md
LEVEL 2  AUTHORITY   the one milestone document relevant to NEXT
LEVEL 3  HISTORY     only the archive section a task needs, via docs/m62/HISTORY_INDEX.md
LEVEL 4  ARCHIVE     full read — audit, migration or root-cause work ONLY
```

**Do NOT read by default:** the historical archive `PROGRESS_THROUGH_S3N.md` (516,784 bytes) ·
every milestone document · **the task bodies of ANY holdout, spent or not — `eval-v4`
included, and being spent is not permission; `eval-v5` above all, retired and unread, as is
the S3W.1 material that exposed one** · raw model responses, none persisted (only
`response_sha256`). **Read history only when** a referenced invariant cannot be resolved from
current authority. **Every high-impact session begins:** Git authority → verifier → current
state → the milestone document NEXT names → task-specific authority → *only then* writes.

---

## 12 — EXACT NEXT

> **ONE single-use human `TRAIN:<plan-hash>` for candidate 005, supplied out of band.**
> It authorises **exactly one** training run and nothing else. Candidate 004 stays **HELD**
> and **not promoted**; no evaluation, `eval-v7`, promotion or candidate 006 is authorised
> by it, and none is pre-authorised by anything in this repository.

**What ruling S4B-001 decided, and what it did not.** A human ruled candidate 005 may move
one dial against candidate 004 — `learning_rate` 5e-5 → 2.5e-5 — candidate 005 only,
prospectively, only to that value. It did **not** decide that 2.5e-5 is better, that training
is the indicated remedy, or that candidate 004 is superseded. `RECOMMENDED_REMEDY` is still
**TOOLING**; the candidate is **`TRAINING_EXPERIMENTALLY_ALLOWED_NOT_PROVEN_NECESSARY`**.
Authority: `…S4B_CANDIDATE005_SINGLE_AXIS_DESIGN.md`, record
`state/m62/rulings/0002-s4b-candidate005-learning-rate.json`.

**A ruling is evidence that a human decided, never a capability.** It supersedes exactly one
clause of one generation-16 entry — *designing, naming or configuring a candidate 005* — and
**erases nothing**; that entry's `eval-v7` clause is **untouched and still barred**. Reading
either learning-rate ruling as general is ruled out: S3U's was candidate 004 only to 5e-5,
S4B's is candidate 005 only to 2.5e-5.

**The HOLD on candidate 004 stands.** S3Y answered *did it clear the frozen eligibility
bar?* — **yes**; it did **not** answer *is it better*: 14 regressed against 9 improved,
**0 security regressions**, paired mean delta **+0.0751**, CI95 **[−0.0040, +0.1793]**, which
does **not** exclude a regression. Figures: `…S3Y_…`, `…S3Z_…`. `HOLD` is a governance
decision, not a candidate state: none was invented, and the only legal move out of
`EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` remains `→ PROMOTED` under
`HUMAN_PROMOTION_AUTHORITY`, **which does not exist**.

**No second look, and no fresh corpus.** `eval-v6` is `USED_IMMUTABLE`: no rerun, re-score,
alternate seed, partial replay, ablation, manual task inspection, qualitative review or
threshold experimentation. `eval-v4` is spent, `eval-v5` is RETIRED unspent with `spent_by`
null, and **no fresh eligibility corpus exists**. **A holdout author is never its
evaluator:** the session that designs and trains candidate 005 is permanently disqualified
from authoring or running its exam, so its evaluation needs a **fresh independent session**,
a fresh holdout it did not write, and a **separate human roadmap ruling**.

**Explicitly NOT authorised** — `next_milestone.ruled_out` in the **generation-17** snapshot
is the authority and is longer than this list: promotion, activation, registry mutation,
merge, tag, release or version bump · **`eval-v7`** or any replacement holdout · **candidate
006** · evaluating any candidate · a second run, seed or value of the axis · retraining,
resuming, re-seeding or patching candidate 003, 004 **or 005** · any epoch, rank, alpha,
dropout or module change · a second axis · `train-v3` · changing gates, graders, thresholds or
the refusal detector · reading `v4`/`v5`/`v6` bodies · raising a reviewed budget or deleting
recorded defects, limitations or invariants to make room · fixing **D39** as a rider.

**PROSE_CANNOT_GRANT_AUTHORITY.** Neither this file, the snapshot, a ruling nor a milestone
document authorises any of the above. `TRAIN_AUTHORITY`, `EVAL_AUTHORITY` and
`PROMOTION_AUTHORITY` are all **NONE**, and a spent authority is historical evidence, never
a capability.

---

## 13 — History

**Immutable archive** `jarvis/docs/m62/history/PROGRESS_THROUGH_S3N.md` · SHA256
`e0914054da4dde4b785bbdabc45a40e0f8b590c2aa3612e9432c685c0c79c1bf` · 516,784 bytes · 6,089
lines · **Router** `jarvis/docs/m62/HISTORY_INDEX.md` · **Migration record**
`jarvis/docs/V69_M62_S3N1_CONTROL_PLANE_V2_ZERO_TRUST_MIGRATION.md`.

The archive is the **byte-for-byte** pre-migration `PROGRESS.md`, **append-never-edit**, its
digest pinned in the snapshot *and* the migration manifest and compared against the bytes by
the verifier. **If it fails, the remediation is operator review — never "update the expected
hash".** Use the history index to find a milestone, defect or era **without reading the
archive in full**; deep milestone documents remain the deep authority.

---

## 14 — Emergency recovery

If `verify_m62_control_plane.py` fails:

**Allowed immediately, all read-only:** read the verifier's problem list · compare archive and
snapshot against Git history (`git log --oneline -- state/m62 PROGRESS.md`,
`git show <commit>:<path>`) · inspect the snapshot chain by hand · read
`V69_M62_S3N1_CONTROL_PLANE_V2_ZERO_TRUST_MIGRATION.md`.

**Forbidden without an explicit operator-authorised CONTROL-PLANE RECOVERY milestone:**
rewriting state, regenerating the archive or updating an expected hash · deleting or editing
a failing snapshot · skipping, weakening or "fixing" the verifier to make it pass · continuing
with TRAIN, EVAL, promotion, merge, tag or release.

**Recovery principle: FAIL CLOSED.** Do not automatically repair state. A verifier that has
been adjusted until it passes has verified nothing.

---

## 15 — Update protocol for this file

Control Plane V2 separates five roles, and they must not collapse back into one: **CURRENT
control state** (this file + `state/m62/current.json` + the latest snapshot) · **DEEP
authority** (the milestone documents in `jarvis/docs/`) · **HISTORICAL event log** (the
immutable archive) · **NAVIGATION** (`jarvis/docs/m62/HISTORY_INDEX.md`) · **TRUST BOUNDARY**
(`jarvis/scripts/verify_m62_control_plane.py`).

**A normal milestone close may update** the current status, NEXT, the active defect and
limitation lists, the test baseline and the READ-FIRST pointers — and **must not append the
milestone report**. Deep detail goes into a milestone document; the index gains one row.

**A state-bearing milestone** (anything that changes a candidate state, a dataset state, a
policy identity, the test baseline, the authority observation or NEXT) additionally writes a
**new snapshot generation**:

1. run the verifier and require `PASS` **before** writing anything;
2. confirm the current generation is the one you expected — if it is not, **stop**;
3. write `state/m62/snapshots/000N-<label>.json` with `state_generation = N`,
   `parent_snapshot_sha256 =` SHA-256 of the previous snapshot's canonical bytes, and
   `subject_state_commit` = the commit that milestone closed at;
4. point `state/m62/current.json` at it; never revise a superseded snapshot;
5. run the verifier again and require `PASS`.

**Size budgets, enforced by the verifier.** This file: **760 lines / 40,960 bytes**, and a
test additionally requires **150 lines of headroom** — so a close that grows the file must
**recompact** it back under **610 lines**, folding superseded detail into the milestone
document that owns it. The snapshot: **34,816 bytes** (migrated from 32,768 at S3X.0 by
operator ruling, with **≥1,024 bytes** of required headroom). `current.json`: **2,048**. The
history index: **32,768**. Raising a budget is an explicit control-plane migration decision,
never a side effect of a normal milestone.

**Never delete a historical negative result, and never rewrite a failed experiment as though
it did not happen.** Mark superseded statements as superseded and name what superseded them.
