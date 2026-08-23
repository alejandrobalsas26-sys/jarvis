# JARVIS V69 — M62 CURRENT CONTROL PLANE

> **This file is CURRENT STATE ONLY. It is not the event log.**
> Everything M62 has ever done through **S3N** is preserved byte-for-byte in the immutable
> archive below. This file describes only what is true *now*, and it is deliberately small
> enough to read at the start of every session.

| | |
|---|---|
| **Control plane** | V2 · schema `m62.control_plane.1` · state generation **11** |
| **Current state (machine-readable)** | `state/m62/current.json` |
| **Latest snapshot** | `state/m62/snapshots/0011-m62-s3w0-candidate004-eval-ready.json` |
| **Snapshot SHA256** | `3c85eadff59a00c37d08161330cbb0c3c630ddfef527308549febcc0ed502603` |
| **Subject state commit** | `4d75d3faa66ccb5388b8e6ced621cc0878f680e3` (S3W.0 qualified the candidate 004 × `eval-v5` ceremony body-free; nothing was evaluated) |
| **Portable training receipts** | `state/m62/receipts/` — tracked, root-independent proof a candidate trained |
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
tokenizer or weights, writes nothing, and opens no socket. It does not trust this file: it
re-derives the policy digests from the production classes, re-hashes the archive and the
snapshot, and asks Git — not prose — about the branch, the ancestry and `master`.

**If it fails, stop.** See §14.

---

## 1 — Checkpoint

| Field | Value |
|---|---|
| Repository | `alejandrobalsas26-sys/jarvis` (`origin`, HTTPS) |
| Branch | `jarvis-v69-m62-training-gym` |
| Subject state commit | `4d75d3faa66ccb5388b8e6ced621cc0878f680e3` — the commit the current snapshot describes |
| Training source commits | candidate 003 `bac49c4a49194d84fbc7f61656662fdcd54799ca`; candidate 004 `80565d32795fb276df202f6bef46ed38bb2bb7c5`. **Deliberately different from the subject commit** |
| HEAD | a descendant of the subject commit; resolve with `git rev-parse HEAD` |
| Divergence from origin | `0  0` |
| `origin/master` | `3705114228edef2f665be349c5c4429b7b16777a` — **untouched by M62** |
| Merge / tag / release / version bump | **none** — `core/version.py` still declares `MILESTONE = 61`, deliberately |

**Every hash in this file is a current identity, not a restart target.** Start from current
HEAD; do not reset to an earlier M62 checkpoint.

**Control-plane commit vs subject-state commit.** The snapshot describes the repository at the
*subject* commit; a milestone's phase-B commit then adds only control-plane and documentation
files on top of it. That is why the two differ, and why the verifier requires HEAD to
*descend* from the subject rather than equal it.

---

## 2 — Current milestone status

| | |
|---|---|
| Milestone | **V69 M62 — Training Gym** |
| Last state-bearing milestone | **S3W.0** — the candidate 004 × `eval-v5` ceremony qualified body-free. **READINESS, NOT AUTHORITY** |
| Last milestone | **S3W.0** — `V69_M62_S3W0_EVAL_V5_QUALIFICATION.md`. **PASS.** **0 weight loads, 0 generations, 0 eval attempts, 0 holdout spend events, 0 `EVAL` authority created.** No live plan was built |
| Phase | **Between milestones — EVAL_READY.** Three candidates measured and `EVALUATED_NOT_ELIGIBLE`; **candidate 004 is TRAINED and UNEVALUATED** and structurally qualified; `eval-v5` fresh and unspent. **No EVAL authority exists** |
| Live training since S3N | **two runs** — candidates 003 and 004, 40/40 optimizer steps each, both `TRAIN` capabilities spent. **No retry is authorised** |
| Live evaluation since S3N | **one run** — S3Q, candidate 003 against `eval-v4`. One plan, one holdout commit, one terminal event, **72 results, 0 generation errors.** `eval-v4` is `USED_IMMUTABLE` |
| S3Q.0.2 | evidence only — **0 weights loaded, 0 generations, 0 new evaluation attempts, no plan consumed, no figure moved** |
| Next | **S3W.1** in a NEW session, from this HEAD: rebuild `eval-v5`, derive the live plan, token-silent preflight, explicit plan-bound human `EVAL` authority, **one** evaluation |

**What M62 is.** The Training Gym: an offline-first, human-gated pipeline that collects and
grades defensive task episodes, builds immutable leakage-checked datasets, plans and executes
a bounded LoRA fine-tune under a single-use token, and runs a paired baseline-versus-adapter
evaluation over a held-out corpus ending in a *non-effectful* candidate proposal. Exercised
live end to end, twice, on real weights. **Nothing has been promoted.**

**All three candidates are `EVALUATED_NOT_ELIGIBLE`, and each failed differently.**
Candidate 001 bought required refusal and paid in over-refusal; candidate 002 repaired
over-refusal outright and lost every required refusal, failing three of nine security
vetoes. **Candidate 003 is the first to clear security outright** — 0 blockers, 0 new
regressions, 3 improvements, the baseline's one secret leak fixed — and improved task
success and reward, and it is blocked by **one deterministic quality gate**: schema
validity 9/9 → 8/9. **No midpoint is demonstrated**, no ablation was run, and none may be
run against a spent holdout. **Nothing has been promoted.**

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
| `qwen3-06b-lora-quality-live-004` | **`TRAINED_UNEVALUATED`** | `a105e01c…a1c3ecc67` | `train v2` | *not measured* | `V69_M62_S3V_CANDIDATE004_LIVE_TRAINING.md` |

**Candidate 003 is MEASURED and NOT ELIGIBLE.** S3O designed it moving **exactly one**
primary axis — training render reasoning policy `MODEL_DEFAULT` → `DISABLED` — with LoRA
scope `ATTENTION_AND_MLP` and `train v2` unchanged; S3P spent the one authorised `TRAIN`
capability on it (40/40 optimizer steps, one verified 392-tensor adapter); S3Q spent
`eval-v4` on it. Against its **own simultaneously-measured baseline**: security blockers
**0**, new security regressions **0**, security improvements **3**, secret leaks 1 → **0**;
task success 24/36 → **25/36**, reward 0.5461 → **0.5903**; schema validity 9/9 → **8/9**,
which is the gate that blocked it. Paired verdicts 11 improved · 12 unchanged · 10 regressed
· 3 security_improvement. Mean delta **+0.044208**, CI95 **[−0.022359, +0.129413]** — which
does **not** exclude a regression.

**It is NOT approved, NOT promoted and NOT production-ready.** `EVALUATED_NOT_ELIGIBLE` is
terminal, and it improved several measurements while failing the frozen eligibility gate.

**Candidate 004 is TRAINED and UNEVALUATED.** S3U built it from candidate 003's
configuration with **one dial moved** — `learning_rate` 1e-4 → 5e-5 — on an explicit human
operator ruling that superseded only the learning-rate clause of one generation-8 entry,
for this candidate only (§12). Rank 16, alpha 32, `alpha/r` 2.0, dropout 0.05, 2 epochs,
seed 42, `ATTENTION_AND_MLP`, `train v2` and reasoning `DISABLED` are inherited by
construction, not re-typed; the measured semantic diff is exactly `{learning_rate}`. S3V
then spent **one** plan-bound single-use `TRAIN` authority on it: 40/40 optimizer steps,
2/2 epochs, 0 truncations, one verified 392-tensor adapter, **0 generations and 0
evaluation attempts**. Train loss 3.591112; validation 3.406055 → 3.310307 over 12 rows.

**Those losses decide nothing.** They are diagnostic, they are **not** eligibility
evidence, and they may not be used to tune or select. Candidate 004's eligibility is
**UNKNOWN** until `eval-v5` is spent exactly once. `TRAINING_ROOT_CAUSE_CONFIDENCE` stays
**NOT_ESTABLISHED**, **no retry is authorised**, and `eval-v5` stays `FROZEN_UNUSED`.

**Neither claim is this table's to make.** `check_training_receipt` and
`check_evaluation_receipt` re-derive both from the tracked, root-independent receipts in
`state/m62/receipts/` and refuse a snapshot that agrees with a verifier constant while a
receipt is absent or disagrees — the `EVALUATED_*` verdict is re-derived by the **production**
decision function, never read. `config_hash` and `plan_hash` are root-bound: re-derive them,
never paste them. **No retry exists** for either the training or the evaluation.

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
| **`m62-defensive-eval v5`** | holdout | **`FROZEN_UNUSED`** | `e852f462…` | `8c6871b0…` | `287a9fb6…` | **nothing — unspent** |
| `m62-defensive-quality-train v1` | training | `USED_IMMUTABLE` | `9bbac2f0…` | genesis | — | S3H |
| `m62-defensive-quality-train v2` | training | `USED_IMMUTABLE` | `24ceb1e0…` | `9bbac2f0…` | — | S3K |

Full 64-character digests are in the snapshot. Every holdout is 36 tasks, splits 12/12/12,
families 12/9/9/6, classes 12/6/18 — the frozen contract, cell for cell.

**`FROZEN_UNUSED` vs `USED_IMMUTABLE` is a scientific property, not a label.**
`FROZEN_UNUSED` means **no model has ever read it**; `USED_IMMUTABLE` means it is spent and
its results are design input from then on (**D35**). The transition is **one-way** and the
verifier has no edge back — relabelling a spent holdout as fresh is the single most damaging
edit anyone could make to this state — and a missing or unrecognised status is a **failure**,
not a fresh corpus. (The S3J/S3K spelling `FROZEN_UNSEEN` is the same state, archive-only.)

### `eval-v5` — frozen, unspent, candidate-blind

```
dataset_id / version    m62-defensive-eval / v5      status  FROZEN_UNUSED   spent_by null
manifest                e852f4627d4fe631f58ee3d120d5d1a81c94480a1c0b84e590d2b08261043f4c
pack                    287a9fb61e3feab510763d834f77a75c3a016fe27ba4d04a4ac86c588c09fed6
parent                  8c6871b0…  (eval-v4, declared not discovered — D34)
task / prompt / target  cda48cf5…  ·  239c6402…  ·  47dbb2a0…      set digests, body-free
```

Frozen by **S3S before candidate 004 exists** — no identifier, no configuration, no
adapter, no `train-v3`, no axis ruling. Zero overlap with `v1`–`v4` on ids, prompts,
targets and all four hash axes; no pair of the 2,710 compared reaches even the near-duplicate
warning threshold; leakage against both training corpora `clean` over 16 checks with
`semantic_similarity` **`NOT_QUALIFIED`, never clean**; identical manifest, pack and set
digests across three independent build roots; D36 host independence **PASS**. Full body-free
evidence: `jarvis/docs/V69_M62_S3S_EVAL_V5_FREEZE.md`. **Bind it by the digests above — its
task bodies stay unread, and the session that authored them may not design what they will
measure.**

**`eval-v4` is spent** (S3Q, candidate 003 — freeze evidence in
`V69_M62_S3N_FRESH_EVAL_V4_FREEZE.md`, results in
`V69_M62_S3Q_CANDIDATE003_LIVE_HELDOUT_EVALUATION.md`). **Under D35 it is development
evidence and may never decide eligibility again.** Its bodies stay unread: nothing in S3Q,
S3Q.0.2, S3R or S3S opened a prompt, a target or a model response.

---

## 5b — The evaluation ceremony, and the measurement it took

Qualified before `eval-v4` was spent (S3Q.0), evidence chain closed before it (S3Q.0.1),
executed once (S3Q), sealed after (S3Q.0.2). Deep authority, in that order:
`V69_M62_S3Q0_EVAL_CEREMONY_QUALIFICATION.md` · `V69_M62_S3Q01_EVAL_RECEIPT_HARDENING.md` ·
`V69_M62_S3Q_CANDIDATE003_LIVE_HELDOUT_EVALUATION.md` · `V69_M62_S3Q02_SEAL_RECOVERY.md`.
They own the full body-free result and the field-by-field receipt derivation.

**Four events, four different facts**, recorded separately because they fail separately:
`PLAN_CONSUMED` · `HOLDOUT_MODEL_FACING_COMMITTED` · `EVALUATION_COMPLETED` ·
`TERMINAL_LEDGER_RECORDED`. Collapsing any pair re-spends a holdout or reports a lost record
as success. S3Q recorded **exactly one of each**, under **one** plan hash.

**PROSPECTIVE SPEND RULE.** A holdout is `USED_IMMUTABLE` the moment the evaluator **durably
commits** the first held-out request to the model-facing boundary — after request parity,
immediately before the first `backend.generate`. Once that line is written the holdout is
spent whatever happens next, and **RERUN IS FORBIDDEN**: not for a crash, a failed artefact
write, a lost terminal line, or a receipt that will not build. `eval-v4` crossed on
2026-08-18; the ledger carries the three lines to prove it.

**WHAT S3Q MEASURED.** One attempt, 36 tasks, both arms, **72 results, 0 generation errors,
0 missing pairs**, eligibility `not_eligible`, blocking gate `schema_validity_regression`,
**security blockers 0**. Plan `5ef87353…`, report `bf7dd00d…`. Figures are in §4.
**No raw prompt, target or response was ever read or persisted.**

**PORTABLE EVALUATION RECEIPT — `m62.eval_receipt.3`** (`scripts/build_m62_eval_receipt.py`):
deterministic, body-free, atomically written, and the **only** thing that may carry an
`EVALUATED_*` state out of a gitignored runtime tree. `.2` refused the real measurement three
times; all three were receipt defects (**D40 · D41 · D42**), closed by moving the contract to
the evidence. Eligibility is **re-derived** by production `decide_eligibility`, never copied.

**THE MEASUREMENT WITNESS** (`state/m62/witnesses/0001-s3q-live-measurement-witness.json`,
`m62.measurement_witness.1`) was committed **alone** at `98ff42a…` whose **first parent is the
evaluation source `c2c025e…`**. **It is not a receipt:** it grants no state, authorises no
retry, promotes nothing, and establishes **repository provenance, NOT hardware-level
execution attestation** — nothing is signed and no PKI is implied.

**Body boundaries.** `ORCHESTRATOR_SEMANTIC_ACCESS` forbidden · `BODY_OPAQUE_PROGRAMMATIC_ACCESS`
permitted for reviewed hashing/validation code · `MODEL_FACING_ACCESS` is the spend.
`task-pack.jsonl` is **`BODY_BEARING`** by design; every other artefact, every ledger line, the
witness and the receipt are **`BODY_FREE`**.

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
*alone* hashes to `1b4696d6…` because it carries library defaults (`timeout_s` 120, `seed` 0,
auto-safe device and precision); `c6b0b682…` is the **configured** policy the sealed S3I and
S3L config documents declare (`timeout_s` 300, `seed` 11, `cpu`, `fp32`). Both are correct,
they are different objects, and the verifier requires them to differ. `config_hash` and
`plan_hash` bind `output_root_id`, runtime and hardware evidence: **they are root-dependent —
re-derive them on the executing host, never paste a recorded value in.**

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
  equivalent to the Windows runtime that produced 001's adapter, and no model has been
  generated under the D38 instrument.
- `openai` is a declared base dependency absent from the system interpreter; its absence
  alone fails 62 tests in three files. Environmental, reproduced at pristine HEAD.
  **Never reconcile test counts across interpreters by arithmetic.**
- **`m62.eval_receipt.3` has described exactly ONE real evaluation**, and `.2` still none.
  A synthetic qualification is evidence about the machinery, never about a candidate.
- **The receipt claims less than it may appear to.** `evaluation_source` binds the commit
  that measured **through the pre-repair witness and its Git first parent** — repository
  provenance, not proof of which bytes executed. `seal_implementation_source` is HEAD at
  build and is named as such. `receipt_hash` proves payload integrity only — not identity,
  authorisation, a signature or who ran the command; authenticity comes from the receipt
  being tracked and from the control-plane hash chain. **Nothing here is signed.**
- **The seal was built AFTER the measurement**, from artefacts that already existed. It
  proves what those artefacts say, not that nobody could have touched them in between.
- **The build-time adapter cross-check needs the runtime tree to still exist.** Afterwards the
  three-way snapshot/training-receipt/evaluation-receipt binding carries the history without
  it, but a receipt cannot be built later on a host whose run directory is gone.
- **`STALE_STATE` detection remains PARTIAL.** The portable receipts close the gap for
  candidates 003 and 004 specifically; runtime artefacts are still outside Git.
- **Candidate 004 is trained, not measured.** Training completed and the adapter is
  structurally valid; that is the whole claim. Its train/validation loss is diagnostic, is
  **not** eligibility evidence, and its eligibility is **UNKNOWN** until `v5` is spent once.

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
- **An `EVALUATED_*` state is REDERIVED, never read.** The portable receipt carries the
  body-free gate, bootstrap, empirical-status and serialisation-state evidence the decision
  was made from, and the **production** `decide_eligibility` is asked what they conclude.
  **There is exactly one eligibility algorithm**, and a receipt that merely *states* a
  verdict evidences nothing.

---

## 9 — Authority observation

```
train_authority       NONE_OBSERVED_IN_REPOSITORY
eval_authority        NONE_OBSERVED_IN_REPOSITORY
promotion_authority   NONE_OBSERVED_IN_REPOSITORY
control_plane_can_grant_authority   FALSE
```

**One `TRAIN` capability was created and consumed in S3P and is now spent.** No reusable
capability exists, no replacement may be minted for candidate 003, and the observation
above is unchanged by that: a spent single-use token is not an authority anyone still
holds.

**This is an OBSERVATION, never a grant.** Plan tokens live outside the repository by
invariant, so it is *measured* as "no tracked file carries a token literal" — the verifier
scans on every run — and is **not** proof that none exists elsewhere. Absence of evidence is
not a clean measurement; that is the D38 lesson, written into the schema. TRAIN, EVAL,
promotion, registry mutation and release remain governed **exclusively** by the single-use
plan-token mechanism plus an explicit human operator decision, which no milestone since has
moved, replaced or weakened.

### Operations requiring new explicit operator authorisation

| Operation | Why |
|---|---|
| Any live training | a fresh plan and a fresh single-use `TRAIN:` token |
| Any live evaluation | a fresh generation, a fresh plan and a fresh single-use `EVAL:` token |
| Registry mutation, promotion, activation, role assignment, adapter merge | no authority in this repository grants these |
| Merging M62, tagging, releasing, bumping `core/version.py` | M62 closure is an explicit operator decision |
| Installing dependencies or touching the global environment | the no-install invariant |
| Any network or model-hub contact | the pipeline is offline-first by invariant |

---

## 10 — Authoritative test baseline

```
invocation      pytest -k m62 --ignore=tests/test_live_brain_v61.py
run from        jarvis/   (repository system interpreter)
result          4234 passed · 20 skipped · 0 failed        [S3V]
```

S3V added no net tests: it rescoped five and strengthened `check_training_receipt`. Counts
are **one** interpreter's; **never reconcile counts across interpreters by arithmetic.**

**Rescoped assertions are not regressions, and there is a documented pattern for them.**
An assertion that compares a *sealed* milestone's property against *live* state also
asserts, silently, that no later generation exists — true by coincidence until the next
milestone writes one. Such tests are pinned to the generation that recorded the property,
and each rescoping is argued in full in its own milestone document. **S3U rescoped two**
unknown-candidate probes, `004` to `005`. **S3V rescoped five**: three "candidate 004 has no
run/receipt/adapter" assertions became "it was trained **exactly once**"; the S3U
control-plane file now reads generation 9 **by path**, not the live pointer; and S3S's
candidate-blindness check reads the sealed freeze commit, where it is true forever.

**Known invocation-context artefact — do not rediscover it as a regression.** Running `pytest`
from the **repository root** instead of `jarvis/` fails **8** tests in
`jarvis/tests/test_training_gym_m62_s3g2_validation_wiring.py`: they read production source
by a path relative to the working directory, so they resolve only from `jarvis/`. S3N
reproduced the same 8 at the pre-S3N commit; the file is unchanged since the subject commit.
**It is distinct from D39**, which fails **4** tests in the *dataset-exports* file when
the validation-wiring file is collected first. Do not conflate them or fix as a rider.

---

## 11 — READ FIRST — the bootstrap contract

A normal session reads **four things**, in this order:

```
LEVEL 0  VERIFY      python jarvis/scripts/verify_m62_control_plane.py
LEVEL 1  CURRENT     state/m62/current.json
                     the snapshot it points at
                     PROGRESS.md   (this file)
LEVEL 2  AUTHORITY   the one milestone document relevant to NEXT
LEVEL 3  HISTORY     only the specific archive section a task actually needs,
                     located through jarvis/docs/m62/HISTORY_INDEX.md
LEVEL 4  ARCHIVE     full read — audit, migration or root-cause work ONLY
```

**Do NOT read by default:** the historical archive `PROGRESS_THROUGH_S3N.md` (516,784 bytes,
and it is history) · every milestone document · **the task bodies of any holdout, spent or
not — `eval-v4` included, and being spent is not permission; `eval-v5` above all, which is
frozen and unread** · raw model responses, none of which were ever persisted (only
`response_sha256`). **Read history only when** a
referenced invariant cannot be resolved from current authority. **Every high-impact session
begins:** Git authority → control-plane verifier → current
state → the milestone document NEXT names → task-specific authority → *only then* writes.

---

## 12 — EXACT NEXT

> **`eval-v5` QUALIFICATION AND A TOKEN-SILENT `EVAL` PREFLIGHT FOR CANDIDATE 004, IN A NEW
> SESSION.** Candidate 004 is **trained and unevaluated**: an adapter and a training receipt
> exist, **no measurement does**. **`eval-v5` stays frozen and unspent**, and no `EVAL`
> authority exists. **Do not evaluate in the session that trained it.**

**Both prior rulings are CLOSED.** A human operator ruling permitted candidate 004 to be
**designed** at **5e-5**, superseding **one clause of one** generation-8 `ruled_out` entry —
the learning-rate clause — **prospectively, for candidate 004 only**; epoch, rank, alpha and
dropout changes stay ruled out, and the historical entry remains factual at the generation
that made it. A second, plan-bound human authority then permitted **exactly one** training
run, and it is **SPENT**. Neither carries forward: the rulings are recorded body-free at
`state/m62/rulings/`, phrases withheld and carried as digests. **No retry is authorised.**

**Keep analysis and authority apart.** S3S.1 §10 *ranked* lower update magnitude first among
candidate 004 axes — analysis, which authorised nothing. Each ruling is a separate human
decision, and the repository records both, never as one thing.

**What candidate 004 is: §4, in full.** One dial, everything else inherited by construction.
It remains a **hypothesis**: the learning rate had never been varied here, so candidate 004
adds one point, not a dose-response curve, and S3S.1 rated the risk to candidate 003's three
security gains from a weaker update as **HIGH**. Losing them would be an informative result.
**Its training and validation loss decide nothing** — they are diagnostic, they are not
eligibility evidence, and they may not be used to tune it.

**Candidate 003 is closed as `EVALUATED_NOT_ELIGIBLE`** — not approved, not promoted, not
production-ready. That state is **terminal**, and `eval-v4` cannot be reused for an
ablation, a re-score or any reason at all.

**The remaining sequence, every arrow a separate human decision:** a token-silent preflight →
a fresh single-use `EVAL` authority at a new generation → **one** paired run against `v5` →
an `m62.eval_receipt.3` → a new generation. Promotion is a further decision no authority
here grants.

**Comparability, before anyone builds a table.** 001, 002 and 003 were measured on `v2`,
`v3` and `v4` — zero shared instances — and 003 onward is fitted under a different training
representation; `v5` shares none with any of them. **They may not be ranked in one table:**
each compares only against its own simultaneously measured baseline, under identical
generation, metric and gate policy digests. **D28, D29 and D33 bound every one.**

**Still explicitly ruled out** — the machine-readable list is `next_milestone.ruled_out` in the generation-10 snapshot and it is the authority; the headline items are: any **epoch, rank, alpha or dropout** change · reading candidate 004's learning-rate permission as **general** · a second axis, `ATTENTION_ONLY` or any module-surface change · any dial slaved to the learning rate · `train-v3` or any `train-v2` modification · raising `max_new_tokens` · changing gates, graders, thresholds or the refusal detector, or turning a **D38/D43** diagnostic into a gate · mutating frozen `eval-v5` · reusing `eval-v4` or reading `v4`/`v5` bodies · retraining, resuming or further fine-tuning candidate 003 **or 004**, or calling either approved · tuning candidate 004 from its loss · evaluating it without a fresh `EVAL` authority or in the session that trained it · ranking 001-004 in one table · fixing **D39** as a rider · promotion, activation, registry mutation, merge, tag, release or version bump.

---

## 13 — History

| | |
|---|---|
| **Immutable archive** | `jarvis/docs/m62/history/PROGRESS_THROUGH_S3N.md` |
| SHA256 | `e0914054da4dde4b785bbdabc45a40e0f8b590c2aa3612e9432c685c0c79c1bf` |
| Size | 516,784 bytes · 6,089 lines |
| **Router** | `jarvis/docs/m62/HISTORY_INDEX.md` |
| **Migration record** | `jarvis/docs/V69_M62_S3N1_CONTROL_PLANE_V2_ZERO_TRUST_MIGRATION.md` |

The archive is the **byte-for-byte** pre-migration `PROGRESS.md`, **append-never-edit**, its
digest pinned in the snapshot *and* the migration manifest and compared against the bytes by
the verifier. **If it ever fails, the remediation is operator review — never "update the
expected hash".** Use the history index to find a milestone, a defect or an era **without
reading the archive in full**; deep milestone documents remain the deep authority.

---

## 14 — Emergency recovery

If `verify_m62_control_plane.py` fails:

**Allowed immediately, all read-only:** read the verifier's problem list · compare the archive
and the snapshot against Git history (`git log --oneline -- state/m62 PROGRESS.md`,
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

Control Plane V2 separates five roles, and they must not collapse back into one:

| Role | Home |
|---|---|
| CURRENT control state | this file + `state/m62/current.json` + the latest snapshot |
| DEEP authority | the milestone documents in `jarvis/docs/` |
| HISTORICAL event log | the immutable archive |
| NAVIGATION | `jarvis/docs/m62/HISTORY_INDEX.md` |
| TRUST BOUNDARY | `jarvis/scripts/verify_m62_control_plane.py` |

**A normal milestone close may update** the current status, NEXT, the active defect and
limitation lists, the test baseline and the READ-FIRST pointers — and **must not append the
milestone report**. Deep detail goes into a milestone document; the index gains one row.

**A state-bearing milestone** (anything that changes a candidate state, a dataset state, a
policy identity, the test baseline, the authority observation or NEXT) additionally writes a
**new snapshot generation**:

1. run the verifier and require `PASS` **before** writing anything;
2. confirm the current generation is the one you expected — if it is not, **stop**;
3. write `state/m62/snapshots/000N-<label>.json` with `state_generation = N`,
   `parent_snapshot_sha256 = ` SHA-256 of the previous snapshot's canonical bytes, and
   `subject_state_commit` set to the commit that milestone closed at;
4. update `state/m62/current.json` to point at it;
5. never revise a superseded snapshot;
6. run the verifier again and require `PASS`.

**Size budgets, enforced by the verifier.** This file: **760 lines / 40,960 bytes**, and a
test additionally requires **150 lines of headroom** — so a close that grows the file must
**recompact** it back under ~610 lines, folding superseded detail into the milestone document
that owns it. The snapshot: **32,768 bytes**. `current.json`: **2,048 bytes**. The history
index: **32,768 bytes**. Migrated sizes are in
`state/m62/migrations/0001-control-plane-v2.json`. Raising a budget is an explicit
control-plane migration decision, never a side effect of a normal milestone.

**Never delete a historical negative result, and never rewrite a failed experiment as though
it did not happen.** Mark superseded statements as superseded and name what superseded them.
