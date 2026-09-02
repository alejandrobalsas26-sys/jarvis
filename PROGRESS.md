# JARVIS V69 — M62 CURRENT CONTROL PLANE

> **This file is CURRENT STATE ONLY. It is not the event log.**
> Everything M62 has ever done through **S3N** is preserved byte-for-byte in the immutable
> archive below. This file describes only what is true *now*, and it is deliberately small
> enough to read at the start of every session.

| | |
|---|---|
| **Control plane** | V3 · schema `m62.control_plane.3` · state generation **27** |
| **Current state (machine-readable)** | `state/m62/current.json` |
| **Latest snapshot** | `state/m62/snapshots/0027-m62-s4f-eval-v7-spent.json` |
| **Snapshot SHA256** | `bcf54ae6263a5c362e874a6e5f0e2a8a0c6f527e3db7809bf75409380559e736` |
| **Subject state commit** | `86bf4c56a74abe9009fcb5e72bf9c66a2af93dfb` (eval-v7 spent once; candidate 005 measured, not eligible, not promoted) |
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
re-derives policy and instrument identities from the production classes, re-hashes the
archive and snapshot, and asks Git — not prose — about branch, ancestry and `master`.

**If it fails, stop** (§14).

---

## 1 — Checkpoint

| Field | Value |
|---|---|
| Repository | `alejandrobalsas26-sys/jarvis` (`origin`, HTTPS) |
| Branch | `jarvis-v69-s4h-eval-instrument-hardening` — declared by gen 28; gen 27 sealed eval-v7 on `jarvis-v69-eval-v7-run` |
| Subject state commit | `924e4664fc383735ec10393660775c0727aac876` — the commit the current snapshot describes |
| Training source commits | 003 `bac49c4a…` · 004 `80565d32…` · 005 `08a7e81f157184389ef14d54007478076314c434`. **Deliberately different from the subject commit** |
| HEAD | a descendant of the subject commit; resolve with `git rev-parse HEAD` |
| Divergence from origin | `0  0` |
| `origin/master` | `3705114228edef2f665be349c5c4429b7b16777a` — **untouched by M62** |
| Merge / tag / release / version bump | **none** — `core/version.py` still declares `MILESTONE = 61`, deliberately |

**Every hash here is a current identity, not a restart target.** Start from current HEAD; do
not reset to an earlier M62 checkpoint. **Control-plane commit vs subject-state commit:** the
snapshot describes the repository at the *subject* commit, and a milestone's phase-B commit
adds only control-plane and documentation files on top — which is why the two differ, and why
the verifier requires HEAD to *descend* from the subject rather than equal it.

---

## 2 — Current milestone status

| | |
|---|---|
| Milestone | **V69 M62 S4H — future evaluation instrument hardening** (M64.1 runtime is frozen infrastructure here and was not touched) |
| Last state-bearing milestone | **S4E** — one paired attempt on `eval-v7` under ONE human `EVAL` authority bound to plan `54488fb3…`: 36+36 generations, ONE spend, terminal `completed`. S4H moved no candidate, dataset or policy identity |
| Last milestone | **S4H** — generation 28, `state/m62/snapshots/0028-m62-s4h-instrument-hardening.json` `def4b272839042cfeaac19dcfb0b29bdad32fdbd884fb158e65f63a0bc956438`. **FUTURE instruments only**: D45–D48 recorded, the four frozen scorer digests re-derive unchanged, 005 **not rescored**, `eval-v7` **not reopened**, **0** loads / generations / authorities. S4F (gen 27) sealed the result; receipt `769d327a…`. `…S4H_EVALUATION_INSTRUMENT_HARDENING.md` |
| Phase | **MEASURED, NOT ELIGIBLE, NO EXAM LEFT.** 001–003 and **005** `EVALUATED_NOT_ELIGIBLE`; **004 stays `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` under its HOLD, not promoted**; `eval-v4`, `v6`, `v7` `USED_IMMUTABLE`; `eval-v5` frozen and retired |
| Live training since S3N | **three runs** — candidates 003, 004 and 005, 40/40 optimizer steps each, all three `TRAIN` capabilities spent. **No retry is authorised** |
| Live evaluation since S3N | **three runs** — S3Q (003 × `v4`), S3Y (004 × `v6`), S4E (005 vs 004 × `v7`, Protocol V4). One plan, one holdout commit and one terminal event each; all three `USED_IMMUTABLE`, **no rerun possible** |
| Next | **A separate human governance decision.** The axis is closed and no holdout remains: a further measurement needs a NEW corpus authored by a session that will not run it. The instruments are readier; **readiness is never authority**. **No `TRAIN`, `EVAL` or promotion authority exists in this repository** |

**What M62 is.** The Training Gym: an offline-first, human-gated pipeline that grades
defensive episodes, builds immutable leakage-checked datasets, runs a bounded LoRA fine-tune
under a single-use token, and runs a paired evaluation over a held-out corpus — base-vs-
adapter, or since S4D adapter-vs-adapter — ending in a *non-effectful* proposal.


**Candidates 001–003 and 005 are `EVALUATED_NOT_ELIGIBLE`, each failing differently** — 001
over-refused; 002 lost every required refusal, failing three of nine security vetoes; **003
clears security outright** but is blocked by **one deterministic quality gate**; 005 won on
quality and introduced **one new secret leak**, which the frozen veto refuses whatever the
delta says. **004 cleared every gate** and is held. **No midpoint is demonstrated**, no
ablation may run against a spent holdout, **nothing is promoted.** Detail: §4.

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

The S3E.2 evaluation load was observed at **bfloat16** because the backend does not force
FP32 (D46); that is history and is not rewritten. `tokenizer_chat_template_hash` digests the
template **source**, not the call — read `chat_render_policy_hash` for the call (D37).

---

## 4 — Candidates

| Candidate | State | Adapter SHA256 | Trained on | Measured on | Deep authority |
|---|---|---|---|---|---|
| `qwen3-06b-lora-quality-live-001` | `EVALUATED_NOT_ELIGIBLE` | `43213035…e22ac858` | `train v1` | `eval v2` | `…S3I_LIVE_QUALITY_…md` |
| `qwen3-06b-lora-quality-live-002` | `EVALUATED_NOT_ELIGIBLE` | `319c2524…f9665409` | `train v2` | `eval v3` | `…S3L_SECOND_QUALITY_…md` |
| `qwen3-06b-lora-quality-live-003` | **`EVALUATED_NOT_ELIGIBLE`** | `6ccd8fdc…c76ce4ea6` | `train v2` | **`eval v4`** | `…S3Q_CANDIDATE003_…md` |
| `qwen3-06b-lora-quality-live-004` | **`EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW`** | `a105e01c…a1c3ecc67` | `train v2` | **`eval v6`** | `…S3Y_CANDIDATE004_…md` |
| `qwen3-06b-lora-quality-live-005` | **`EVALUATED_NOT_ELIGIBLE`** | `52d6da26…52688f2a` | `train v2` | **`eval v7`** | `…S4F_CANDIDATE005_…md` |

**Candidate 003 is MEASURED and NOT ELIGIBLE.** S3O moved **exactly one** primary axis —
render policy `MODEL_DEFAULT` → `DISABLED`; S3P trained it; S3Q spent `eval-v4`. It **clears
security outright** and is blocked by one deterministic quality gate, schema validity 9/9 →
**8/9**; its mean delta does **not** exclude a regression. **`EVALUATED_NOT_ELIGIBLE` is
terminal**: NOT approved, NOT promoted, NOT production-ready.

**Candidate 004 is MEASURED, ELIGIBLE and HELD.** S3U built it from 003's configuration with
**one dial moved** — `learning_rate` 1e-4 → 5e-5 — on an explicit human ruling, that
candidate only. S3V spent **one** `TRAIN` authority: 40/40 steps, 2/2 epochs, one verified
392-tensor adapter. S3Y spent `eval-v6` **once**; every gate passed, security regressions
**0**, operator decision **HOLD**. **Not promoted**, **no retry**, and S4D binding it as the
`eval-v7` REFERENCE arm changes none of that.

**Candidate 005 is MEASURED and NOT ELIGIBLE — on a SECURITY VETO.** S4B built it from 004's
configuration with **one dial moved** — `learning_rate` 5e-5 → 2.5e-5 — on ruling
**S4B-001** (§12); everything else is inherited **by construction, not re-typed**, and the
measured semantic diff is exactly `{learning_rate}`. S4C spent one `TRAIN` authority; S4E
spent `eval-v7` **once** under Protocol V4, 004 as REFERENCE. Three statements, and
collapsing any two is wrong. **(A)** It **won on quality**: mean paired delta **+0.1714**,
95% CI **[+0.0566, +0.3122]** excluding the regression margin, 6 refusal failures fixed
(critical safety violations 11 → 5). **(B)** It introduced **1 new secret leak** where the
reference arm produced **0**. **(C)** The frozen policy vetoes any new security regression
whatever the delta says — `security_is_a_veto_not_a_weight`. So: 3 blocking gates, 2 of them
security, **NOT_ELIGIBLE**. Not "005 is simply worse"; not "005 won, so weigh the leak". The
**median** delta is **+0.0013** — the mean is carried by six pairs flipping 0→1.
`…S4F_CANDIDATE005_…md`.

**A training loss still decides nothing**, and neither does a mean delta. **No exam remains**
(§5), and `TRAINING_ROOT_CAUSE_CONFIDENCE` stays **NOT_ESTABLISHED**.

**No claim here is this table's to make.** `check_training_receipt` and
`check_evaluation_receipt` re-derive them from the tracked, root-independent receipts in
`state/m62/receipts/`, refusing a snapshot that agrees with a verifier constant while a
receipt is absent or disagrees; the `EVALUATED_*` verdict comes from the **production**
decision function, never read. `config_hash` and `plan_hash` are root-bound: re-derive, never
paste. **No retry exists.**

**Closed candidate-state vocabulary.** `NOT_CREATED` · `DESIGNED_UNTRAINED` ·
`TRAINED_UNEVALUATED` · `EVALUATED_NOT_ELIGIBLE` · `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` ·
`EVALUATED_NEEDS_MORE_EVIDENCE` · `EVALUATED_QUARANTINED` · `PROMOTED`. Anything else is
refused, and the verifier rejects every transition absent from its table — including
`TRAINED_UNEVALUATED → PROMOTED`. `EVALUATED_NOT_ELIGIBLE` is **terminal**. **`PROMOTED` is
refused outright in a snapshot:** no promotion mechanism exists, so nothing could witness it.

---

## 5 — Datasets

| Dataset | Role | State | Manifest | Parent | Pack | Spent by |
|---|---|---|---|---|---|---|
| `m62-defensive-eval v1` | holdout | `USED_IMMUTABLE` | `0970600c…` | genesis | `d714d89b…` | S3E.2 |
| `m62-defensive-eval v2` | holdout | `USED_IMMUTABLE` | `82b60bfd…` | `0970600c…` | `3744a22e…` | S3I, cand 001 |
| `m62-defensive-eval v3` | holdout | `USED_IMMUTABLE` | `7c948236…` | `82b60bfd…` | `28d2f7d0…` | S3L, cand 002 |
| `m62-defensive-eval v4` | holdout | `USED_IMMUTABLE` | `8c6871b0…` | `7c948236…` | `95b4e2f6…` | S3Q, cand 003 |
| **`m62-defensive-eval v5`** | holdout | **`FROZEN_UNUSED`** · **eligibility RETIRED** | `e852f462…` | `8c6871b0…` | `287a9fb6…` | **nothing — never spent** |
| **`m62-defensive-eval v6`** | holdout | **`USED_IMMUTABLE`** | `413e6757…` | `e852f462…` | `41579381…` | **S3Y, cand 004** |
| **`m62-defensive-eval v7`** | holdout | **`USED_IMMUTABLE`** | `e80cc46f…` | `413e6757…` | `e6d8d0b2…` | **S4E, 005 vs 004** |
| `m62-defensive-quality-train v1` | training | `USED_IMMUTABLE` | `9bbac2f0…` | genesis | — | S3H, cand 001 |
| `m62-defensive-quality-train v2` | training | `USED_IMMUTABLE` | `24ceb1e0…` | `9bbac2f0…` | — | S3K, S3P; reused UNCHANGED by 004 and 005 |

Full 64-character digests are in the snapshot records. Every holdout is 36 tasks, splits
12/12/12, families 12/9/9/6, classes 12/6/18 — the frozen contract, cell for cell.

**`FROZEN_UNUSED` vs `USED_IMMUTABLE` is a scientific property, not a label.**
`FROZEN_UNUSED` means **no model has ever read it**; `USED_IMMUTABLE` means it is spent and
its results are design input from then on (**D35**). The transition is **one-way**, the
verifier has no edge back, and a missing status is a **failure**, not a fresh corpus.

### `eval-v7` — SPENT ONCE, the first REFERENCE-ADAPTER exam (S4D froze it, S4E spent it)

```
manifest  e80cc46fa0b2c1ec020ed02f9565d778772d8e76dd208f2ba49349ab199b369a
task / prompt / target   a5bc453a…  ·  8226b43a…  ·  d9014520…   set digests, body-free
```

**Authored candidate-blind by S4D.** Freshness against `v1`–`v6`: **0 exact overlaps** on six
identity surfaces, **0 WARN, 0 BLOCK** over 1,138 comparisons, comparator non-vacuous; both
corpora **clean**. S4E spent it under Protocol V4 — 004 **REFERENCE**, 005
**CANDIDATE**, one attempt, one spend, 72 generations, terminal `completed` — and S4F sealed
the result. `USED_IMMUTABLE` is **terminal**: no rerun, no second look, a second spend
refused mechanically. `…S4D_EVAL_V7_FREEZE.md` · `…S4F_CANDIDATE005_…md`.

### `eval-v6` — SPENT ONCE on candidate 004 (S3X.1 froze it, S3Y spent it)

`spent_by` S3Y LIVE, candidate 004 (plan `e2b591fe`, report `d708d721`); manifest
`413e675711d51f5b98cb5a8ec7ff7fb0d8eb36b5e4c6dff790fb60f764f8fba6`; parent `e852f462…`
declared not discovered (**D34**). **Freshness was measured before the spend** — 2,564
comparisons against `v1`–`v5`, **0 overlaps, 0 WARN, 0 BLOCK** — and it was authored
**candidate-blind**. **No rerun or second look is possible.**
`…S3X1_EVAL_V6_FREEZE.md` · `…S3Y_CANDIDATE004_…md`.

### `eval-v5` — frozen, never model-spent, RETIRED from eligibility use

```
dataset_id / version    m62-defensive-eval / v5      status  FROZEN_UNUSED   spent_by null
manifest                e852f4627d4fe631f58ee3d120d5d1a81c94480a1c0b84e590d2b08261043f4c
task / prompt / target  cda48cf5…  ·  239c6402…  ·  47dbb2a0…      set digests, body-free
```

**RETIRED FROM ELIGIBILITY USE at generation 12, still `FROZEN_UNUSED`, `spent_by` null.**
Both halves are true and neither collapses into the other — 0 weight loads, 0 generations, 0
spend events, no receipt, against one pre-authorisation body exposure (**D44**, §7;
retirement rule §8). Its bodies stay **unread**; a later version declaring it an **ancestor**
is lineage, not reuse. `…S3S_EVAL_V5_FREEZE.md` · `…S3X0_PRESPEND_…_RECOVERY.md`.

**`eval-v4` is spent** (S3Q, candidate 003; `…S3N_FRESH_EVAL_V4_FREEZE.md` ·
`…S3Q_CANDIDATE003_…md`). **Under D35 it is development evidence and may never decide
eligibility again.** Its bodies stay unread.

---

## 5b — The evaluation ceremony

Qualified before `eval-v4` was spent (S3Q.0), chain closed before it (S3Q.0.1), executed
once (S3Q), sealed after (S3Q.0.2); `…S3Y_…` owns 004's measurement and `…S4F_…` owns 005's.
Those documents own the full body-free results and receipt derivations (index §13). **Four
events, four different facts** (§8): `PLAN_CONSUMED` · `HOLDOUT_MODEL_FACING_COMMITTED` ·
`EVALUATION_COMPLETED` · `TERMINAL_LEDGER_RECORDED` — each live evaluation recorded
**exactly one of each**, under **one** plan hash.

**PROSPECTIVE SPEND RULE** (§8). A holdout is `USED_IMMUTABLE` the moment the evaluator
**durably commits** the first held-out request to the model-facing boundary — after request
parity, immediately before the first `backend.generate`. Once written it is spent whatever
happens next, and **RERUN IS FORBIDDEN**: not for a crash, a failed artefact write, a lost
terminal line or a receipt that will not build. `v4` crossed 2026-08-18, `v6` at S3Y, `v7` at
S4E; **`v5` never crossed it** — hence `FROZEN_UNUSED`, not spent.

**PORTABLE RECEIPTS.** `m62.eval_receipt.3` and `m62.train_receipt.1` are deterministic,
body-free, atomic, and the **only** things that may carry an `EVALUATED_*` or `TRAINED_*`
state out of a gitignored runtime tree; eligibility is **re-derived** by production
`decide_eligibility`, never copied (`.2`'s refusals were **D40–D42**, §7).
`m62.eval_receipt.4` is S4D's **additive** reference-adapter shape: **no `baseline` field at
all**, so it cannot record an adapter as a bare base model; no prior receipt is migrated.
**THE MEASUREMENT WITNESS** (`state/m62/witnesses/`) is **not a receipt:** it grants no
state, authorises no retry, promotes nothing, and establishes **repository provenance, NOT
execution attestation** — nothing signed, no PKI implied.

**Body boundaries.** `ORCHESTRATOR_SEMANTIC_ACCESS` forbidden — **enforced in memory, not
only on disk (D44)** · `BODY_OPAQUE_PROGRAMMATIC_ACCESS` permitted for reviewed
hashing/validation code · `MODEL_FACING_ACCESS` is the spend. `task-pack.jsonl` is
**`BODY_BEARING`** by design; every other artefact, ledger line, witness, receipt and
instrument finding is **`BODY_FREE`**.

---

## 6 — Policy identities

Re-derived by the verifier from the production classes on every run, never read from this
table. The S4H instrument versions are anchored the same way.

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
and precision); `c6b0b682…` is the **configured** policy the sealed S3I and S3L configs
declare (`timeout_s` 300, `seed` 11, `cpu`, `fp32`). Both are correct, they are different
objects, and the verifier requires them to differ. `config_hash` and `plan_hash` bind
`output_root_id`, runtime and hardware evidence: **root-dependent — re-derive on the
executing host, never paste a recorded value in.** D46: the recorded `cpu`/`fp32` was never
passed to the loader; a future contract fixes that prospectively and changes no digest.

---

## 7 — Open defects and current limitations

Only what still binds operation; D1–D27 live in the archive, indexed in
`jarvis/docs/m62/HISTORY_INDEX.md`. The snapshot's `defects` record carries each in full.

| Defect | State | What it still binds |
|---|---|---|
| **D25** | FIXED | A persisted report in `run_state: comparing` is the documented serialisation state, **not** an unfinished run. |
| **D28** | **OPEN** | No tool-call transport exists, so `tool_call_validity_rate` is **VACUOUS** on both arms and the six `tool_call_schema` tasks decide nothing. Unchanged; FUTURE successor `tool_call_validator/v2`, named by no evaluation. |
| **D29** | ACCEPTED LIMITATION | `looks_like_refusal` reads sixteen literal phrasings the JSON refusal targets do not contain. It bounds QG-1 and SV-5 **in both directions** and travels into `v4` by design. Unchanged; FUTURE successor `refusal_behavior/v2`. |
| **D30** | FIXED | A plan may not report `is_executable` with an unverified model cache. |
| **D31** | FIXED | VALIDATION is wired and **diagnostic only**. Do not remove `eval_dataset` or the closing `trainer.evaluate()`. |
| **D32** | SUPERSEDED | By D34. |
| **D33** | **OPEN** | The declared generation timeout is **not enforced**, so `timeout_rate` is **VACUOUS**. A config must state `timeout_s`; the default is 120 s. |
| **D34** | FIXED | A dataset parent is **DECLARED**, never discovered from disk. Fails closed rather than degrading to genesis. |
| **D35** | OPERATOR RULING | A spent holdout becomes development evidence. **Each candidate needs a fresh holdout.** Not a contamination claim. |
| **D36** | FIXED | The identity redactor matches unless flanked by ASCII letters on both sides. Do not simplify to a substring match; do not widen to `\b`. |
| **D37** | FIXED | Training binds a reasoning policy; `chat_render_policy_hash` binds the *call*. Exercised by one live run (003, S3P, render `8619f96c…`). **Historical causality NOT_ESTABLISHED**; closing it is *not* predicted to restore 9/9. |
| **D38** | FIXED (obs. only) | Output-budget exhaustion is a body-free diagnostic beside the unchanged input-truncation metric. **No gate reads it and none may be added without a separate operator decision.** Reaching the ceiling is not failure. |
| **D39** | **OPEN** | Order-dependent test isolation between the S3G.2 validation-wiring file and the dataset-exports file. No recorded figure was ever affected. **Not a rider fix.** |
| **D40–D42** | FIXED at `.3` | The three refusals `m62.eval_receipt.2` produced. **D40** the paired outcome is **not** an exhaustive `wins/ties/losses` partition — production classifies **four** comparable verdicts and `security_improvement` is **not** a win; **D41** close an encoding question by **defining** the encoding (`U+2212` is legitimate decision text), never by discarding evidence; **D42** the code that **measured** is not the code that **built the receipt**. |
| **D43** | FIXED (obs. only) | `EXTRA_DATA` was indistinguishable from an unclosed document. Fixed **prospectively** at S3T.0 with a body-free class, location and repetition scalar. **No gate reads them**; nothing historical is backfilled. |
| **D44** | **FIXED · GATE** | A held-out body reached an orchestration session **before any authorisation existed**, through representation machinery alone — including `repr` of a **bound method**. The persistence firewall held; the missing one was in-memory display. `schemas.body_free_repr` renders identity and digests only, guarded **by type**. `…S3X0_…md`. |
| **D45–D47** | FIXED (obs. only) | S4H's instrument findings, each fixed **PROSPECTIVELY** and read by **no gate**: **D45** `secret_pii:secret` carried no rule provenance — enough to veto, not to review; **D46** the loader passes no `device_map` and no dtype, so the declared `cpu`/`fp32` never reached the load; **D47** `classify_empirical_status` reads the **quality** denominator, so 36/36 generated and scored reported `partial_live`. **005 not rescored; nothing backfilled.** |
| **D48** | **FIXED** | `-k m62` deselected all 212 tests in three `m63`-named modules asserting M62 state. A filename substring is not a scientific boundary. `state/m62/scientific-suite.json` + its verifier. |

### Limitations that travel into any successor run

- Gate thresholds are **uncalibrated** (`thresholds_are_calibrated: false`). Every holdout,
  `v7` included, is **36 synthetic tasks, one author, one session, no independent review**;
  `tool_call_schema` has only 6, kept vacuous by D28. **Semantic leakage has never run** —
  freshness evidence is exact and lexical only, so a pure paraphrase would not be caught.
- **Candidates 001–004 are not head-to-head comparable.** Each was measured on a different
  holdout with zero shared instances, and one fitted under `DISABLED` is not comparable to
  one that was not; what is comparable is each against its **own** simultaneously-measured
  baseline. `eval-v7` was the first true head-to-head, it is **spent**, and 005-vs-004 is
  the only such figure that exists.
- **Every candidate was measured ONCE.** One host, CPU, one seed, one run; no repeat, second
  host, GPU, dtype control arm or ablation, and `deterministic_reproduction_claimed` is
  `false`. The 12-row train-time validation is steering material, appears in no gate and is
  **not** comparable across candidates. Neither Kali runtime is claimed bytewise equivalent
  to the Windows runtime that produced 001's adapter.
- **Candidate 003's interval does not exclude a regression.** Mean delta +0.044208, CI95
  [−0.022359, +0.129413] over 36 pairs; recorded `regression_not_excluded`, **not** an
  improvement. Its blocking gate is one task moving on a 36-task holdout, indistinguishable
  from noise — and **no ablation may run against a spent holdout**. **The D37 axis is
  neither confirmed nor refuted**; historical causality stays `NOT_ESTABLISHED`.
- `openai` is a declared base dependency absent from the system interpreter; its absence
  alone fails 62 tests in three files. Environmental, reproduced at pristine HEAD. **Never
  reconcile test counts across interpreters by arithmetic.**
- **The receipt claims less than it may appear to.** `m62.eval_receipt.3` has described TWO
  real evaluations, `.4` one, `.2` none, and a synthetic qualification is evidence about the
  machinery, never about a candidate. `evaluation_source` binds the measuring commit
  **through the pre-repair witness and its Git first parent** — repository provenance, not
  proof of which bytes ran; `seal_implementation_source` is HEAD at build; `receipt_hash`
  proves payload integrity only. **Nothing is signed.** It was built **AFTER** the
  measurement from artefacts that already existed, so it proves what they say, not that
  nobody touched them between. **`STALE_STATE` detection remains PARTIAL**: portable receipts
  close the gap for 003, 004 and 005; runtime artefacts are still outside Git.
- **S4H's instruments are FUNCTIONAL, not CALIBRATED** — `REAL_WORLD_CALIBRATED = NO`, every
  case written by the milestone that wrote the detector — and **additive and inert**: no
  config names one, no historical scorer imports one, nothing was rescored.
- **The D44 exposure is PERMANENT** — no fix restores `v5`'s freshness, and it was **not**
  re-measured, because re-opening the material to size it would repeat the disclosure. One
  rendered body is a **floor**, not a proved bound.

The snapshot's `limitations` record carries the full list; this is the operational subset.

---

## 8 — Frozen invariants

- **`PROSE_CANNOT_GRANT_AUTHORITY`.** No document, JSON file, comment or AI response —
  including this one — authorises TRAIN, EVAL, promotion, registry mutation or release.
- **Offline-first execution.** `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`,
  `local_files_only`, `trust_remote_code=false`. Nothing downloads. **Pinned immutable
  revisions only**, never a branch or tag ref.
- **No automatic dependency installation, no global pip mutation.** Optional profiles live
  only in isolated gitignored environments; `.venv-m62-eval-linux` is the runtime of record
  and is immutable.
- **No pickle in adapter or evaluation artefacts** — safetensors only. **No hidden-target
  leakage**: model-facing packs have no field that could hold a target. **No training on
  evaluation-only material**: the pack builder refuses `TRAIN` unconditionally.
- **`TRAIN_SIDE_SPLITS = {TRAIN, VALIDATION}`.** VALIDATION is *steering* material, never
  held-out evidence, and appears in no gate. **Security is a veto, not a weighted tradeoff.**
- **Plan tokens are single-use.** A consumed or failed plan is never replayed, and no token
  literal lives in a tracked file. **`ModelCandidateProposal` is non-effectful**: no registry
  mutation, promotion or activation without a later explicit authority.
- **Runtime artefacts stay gitignored and package-excluded.** **`master` stays untouched
  until explicit M62 closure** — no merge, tag, release or version bump.
- **An immutable artefact is never edited.** A corrected dataset is a **new version** with a
  declared lineage; a superseded snapshot is never revised; the archive is never touched. **A
  dataset lineage is DECLARED, never discovered** from whatever is on disk (D34).
- **Gate policy `e5003319…` is byte-pinned by test.** QG-2 stays absolute, FG-1/FG-2 stay
  baseline-relative, and 7/9 is not "good enough".
- **ONE WRITER PER CONTROL-PLANE GENERATION.** If the current generation is not the one you
  expected, **stop**: no last-write-wins, no automatic snapshot merge. **If the verifier
  fails, do not write, train, evaluate or promote**, and never repair state automatically.
- **ORCHESTRATOR BODY-BLINDNESS IS A GATE.** A held-out body may not reach an orchestration
  session by **any** route — `repr`, `str`, f-string, `%r`, logging, exception text,
  traceback, or `repr` of a bound method. A body-bearing dataclass **must** install a
  body-free `__repr__`; the persistence firewall alone is **not** enough (**D44**).
- **`eval-v5` ELIGIBILITY USE IS RETIRED**, prospectively from generation 12. It stays
  `FROZEN_UNUSED`, `spent_by` null, because **no model ever saw it**; its preregistered
  body-blindness precondition failed pre-authorisation.
- **An `EVALUATED_*` state is REDERIVED, never read.** The portable receipt carries the
  body-free gate, bootstrap, empirical-status and serialisation-state evidence the decision
  was made from, and the **production** `decide_eligibility` is asked what they conclude.
  **One eligibility algorithm**; a receipt that merely *states* a verdict evidences nothing.
- **A FUTURE instrument is pinned by exact version, never resolved as "latest"**, and no
  historical scorer may import one. Its finding carries a rule id and a class, **never a
  matched value** (S4H).

---

## 9 — Authority observation

```
train / eval / promotion            NONE_OBSERVED_IN_REPOSITORY
control_plane_can_grant_authority   FALSE
```

**Three `TRAIN` capabilities created and consumed** — S3P (003), S3V (004), S4C (005) — and
two `EVAL`, S3Y (`v6`) and S4E (`v7`). All spent; no reusable capability exists and no
replacement may be minted. **A spent single-use token is not an authority anyone holds.**

**This is an OBSERVATION, never a grant.** Plan tokens live outside the repository by
invariant, so it is *measured* as "no tracked file carries a token literal" — the verifier
scans every run — and is **not** proof that none exists elsewhere. Absence of evidence is not
a clean measurement; that is the D38 lesson. TRAIN, EVAL, promotion, registry mutation and
release stay governed **exclusively** by the single-use plan-token mechanism plus an explicit
human decision, which no milestone since has moved, replaced or weakened.

### Operations requiring new explicit operator authorisation

| Operation | Why |
|---|---|
| Any live training | a fresh plan and a fresh single-use `TRAIN:` token |
| Any live evaluation | a fresh generation, a fresh plan and a fresh single-use `EVAL:` token |
| Freezing a fresh holdout (`eval-v8`) | a new session that did not design the candidate, plus an explicit human decision; it evaluates nothing |
| Registry mutation, promotion, activation, role assignment, adapter merge | no authority here grants these |
| Merging M62, tagging, releasing, bumping `core/version.py` | M62 closure is an explicit operator decision |
| Touching the global environment, or any network or model-hub contact at run time | the no-global-mutation and offline-first invariants |

---

## 10 — Authoritative test baseline

```
canonical   verify_m62_scientific_suite.py --print-invocation -> pytest <54 modules>  [S4H]
broad       pytest -k m62 --ignore=tests/test_live_brain_v61.py
run from    jarvis/ (repository system interpreter)
result      4728 passed · 24 skipped · 0 failed                              [S4D, broad]
```

**`-k m62` is no longer the authority (D48):** it matches node ids and deselected all **212**
tests in three `m63`-named modules asserting M62 state. Keep the broad sweep too — it is
wider in other directions. S4D adds the Protocol V4 and `eval-v7` suites. Edits to sealed suites moved **witnesses, not properties**; S4D and S4F rescoped
assertions pinning a *version roster* rather than the property they own, on the precedent
S3N, S3S and S3X.1 each set. **0** threshold, gate, grader, policy, receipt or transition
weakenings. Counts are **one** interpreter's; **never reconcile across interpreters.**

**Rescoped assertions are not regressions.** An assertion comparing a *sealed* milestone's
property against *live* state also asserts, silently, that no later generation exists — true
by coincidence until the next milestone writes one. Such tests are pinned to the generation
that recorded the property, and each rescoping is argued in its own milestone document.

**Known invocation-context artefact — do not rediscover it as a regression.** Running `pytest`
from the **repository root** instead of `jarvis/` fails **8** tests in
`…s3g2_validation_wiring.py`, which read production source relative to the working directory;
S3N reproduced the same 8 pre-S3N. **It is distinct from D39**, which fails **4** tests in
the *dataset-exports* file. Do not conflate them or fix as a rider.

---

## 11 — READ FIRST — the bootstrap contract

A normal session reads **four things**, in this order:

```
LEVEL 0  VERIFY     python jarvis/scripts/verify_m62_control_plane.py
LEVEL 1  CURRENT    state/m62/current.json + the snapshot it points at + PROGRESS.md
LEVEL 2  AUTHORITY  the one milestone document relevant to NEXT
LEVEL 3  HISTORY    only the archive section a task needs, via docs/m62/HISTORY_INDEX.md
LEVEL 4  ARCHIVE    full read — audit, migration or root-cause work ONLY
```

**Do NOT read by default:** the historical archive `PROGRESS_THROUGH_S3N.md` · every
milestone document · **the task bodies of ANY holdout, spent or not — `v4` and `v7` included,
and being spent is not permission; `v5` above all, retired and unread, as is the S3W.1
material that exposed one** · raw model responses, none persisted (only `response_sha256`).
**Read history only when** a referenced invariant cannot be resolved from current authority.

---

## 12 — EXACT NEXT

> **A SEPARATE HUMAN GOVERNANCE DECISION.** Candidate 005 is measured and **NOT ELIGIBLE**
> on a security veto. `eval-v7` is spent and immutable, and **no holdout remains**. Nothing
> model-facing is authorised: no re-run, no promotion, no candidate 006, no `eval-v8`.
> **`TRAIN`, `EVAL` and promotion authority are all NONE.** S4H hardened the instruments a
> future run would use and **measured nothing**: readiness is never authority.

**The axis is closed.** A further measurement needs a **new** corpus, authored by a session
that will not run it, plus a fresh single-use authority — neither exists, and a corpus is
readiness, not permission. A successor may study this security regression **body-free**, from
training and non-holdout evidence only; `eval-v7` may never be reopened, and an inconvenient
result buys no exception.

**What the result decided, and what it did not.** It measured one axis once; it did **not**
make candidate 004 promotable, whose HOLD rests on its own `v6` evidence.
`RECOMMENDED_REMEDY` is still **TOOLING** and the candidate still
**`TRAINING_EXPERIMENTALLY_ALLOWED_NOT_PROVEN_NECESSARY`**. Losses decide nothing — the
receipt records `validation_is_held_out_eligibility_evidence: false`. **The S4B, S4C and S4E
authorities are spent**: S4B-001 was `DESIGN_ONLY`, 005 only, only to 2.5e-5, and single-use
tokens are **never replayed** — no second seed, no 005b, no resume, no retry because a result
looks unappealing. Neither learning-rate ruling is general: S3U's was 004 only, S4B's 005
only. `…S4C_…md`.

**The HOLD on candidate 004 stands.** Neither S4C, S4D, S4E nor S4H reopened, re-measured or
reinterpreted it; serving as the `eval-v7` REFERENCE arm does not either. It remains
`EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW`, not promoted; the only legal move out is `→ PROMOTED`
under a `HUMAN_PROMOTION_AUTHORITY` that does not exist. **No second look:** `v4`, `v6` and
`v7` are `USED_IMMUTABLE` — no rerun, re-score, alternate seed, partial replay, ablation,
task inspection, qualitative review or threshold experimentation — and `v5` is RETIRED
unspent, `spent_by` null. Every spent holdout stays **unread**.

**Explicitly NOT authorised** — the **current** snapshot's `next_milestone.ruled_out` is the
authority and is longer than this: promotion, activation, registry mutation, merge, tag,
release or version bump · **candidate 006** · recording 005 as eligible · spending `eval-v7`
twice, running one arm alone, editing or re-freezing it · a second run, seed or value of the
axis · retraining or patching candidate 003, 004 **or 005** · any epoch, rank, alpha, dropout
or module change · a second axis · `train-v3` · changing gates, graders, thresholds or the
refusal detector · reading `v4`–`v7` bodies · raising a budget or deleting recorded defects,
limitations or invariants to make room · **D39** as a rider · **S4H's instruments as a reason
to revisit 005** · **`SYNTHETIC_CALIBRATION` cited as calibration**.

**`PROSE_CANNOT_GRANT_AUTHORITY`** (§8): neither this file, the snapshot, a ruling, a receipt
nor a milestone document authorises any of the above. **A receipt is evidence of an operation
and never authority for another.**

---

## 13 — History

**Immutable archive** `jarvis/docs/m62/history/PROGRESS_THROUGH_S3N.md` · SHA256
`e0914054da4dde4b785bbdabc45a40e0f8b590c2aa3612e9432c685c0c79c1bf` · 516,784 bytes · 6,089
lines · **Router** `jarvis/docs/m62/HISTORY_INDEX.md` · **Migration record**
`jarvis/docs/V69_M62_S3N1_CONTROL_PLANE_V2_ZERO_TRUST_MIGRATION.md`.

The archive is the **byte-for-byte** pre-migration `PROGRESS.md`, **append-never-edit**, its
digest pinned in the snapshot *and* the migration manifest and compared against the bytes by
the verifier. **If it fails, the remediation is operator review — never "update the expected
hash".** Use the index to find a milestone, defect or era **without reading the archive in
full**; milestone documents remain the deep authority.

---

## 14 — Emergency recovery

If `verify_m62_control_plane.py` fails:

**Allowed immediately, all read-only:** read the verifier's problem list · compare archive
and snapshot against Git history (`git log --oneline -- state/m62 PROGRESS.md`,
`git show <commit>:<path>`) · inspect the snapshot chain by hand · read
`V69_M62_S3N1_CONTROL_PLANE_V2_ZERO_TRUST_MIGRATION.md`.

**Forbidden without an explicit operator-authorised CONTROL-PLANE RECOVERY milestone:**
rewriting state, regenerating the archive or updating an expected hash · deleting or editing
a failing snapshot · skipping, weakening or "fixing" the verifier to pass · continuing with
TRAIN, EVAL, promotion, merge, tag or release.

**Recovery principle: FAIL CLOSED.** Never repair state automatically; a verifier adjusted
until it passes has verified nothing.

---

## 15 — Update protocol for this file

Control Plane V2 separates five roles that must not collapse into one: **CURRENT control
state** (this file + `state/m62/current.json` + the latest snapshot) · **DEEP authority**
(milestone documents in `jarvis/docs/`) · **HISTORICAL event log** (the archive) ·
**NAVIGATION** (`jarvis/docs/m62/HISTORY_INDEX.md`) · **TRUST BOUNDARY**
(`jarvis/scripts/verify_m62_control_plane.py`). **A normal milestone close may update** the
current status, NEXT, the active defect and limitation lists, the test baseline and the
READ-FIRST pointers — and **must not append the milestone report**. Deep detail goes to a
milestone document; the index gains a row.

**A state-bearing milestone** (one that changes a candidate or dataset state, a policy or
instrument identity, the test baseline, the authority observation or NEXT) additionally
writes a **new snapshot generation**:

1. run the verifier and require `PASS` **before** writing anything;
2. confirm the current generation is the one you expected — if not, **stop**;
3. write `state/m62/snapshots/000N-<label>.json` with `state_generation = N`,
   `parent_snapshot_sha256 =` SHA-256 of the previous snapshot's canonical bytes and
   `subject_state_commit` = the commit that milestone closed at;
4. point `state/m62/current.json` at it; never revise a superseded snapshot;
5. run the verifier again and require `PASS`.

**Size budgets, enforced by the verifier.** This file: **760 lines / 40,960 bytes**, plus a
test requiring **150 lines of headroom**, so a close that grows it must **recompact** back
under **610 lines**, folding superseded detail into the milestone document that owns it.
Snapshot **34,816 bytes** (migrated from 32,768 at S3X.0 by operator ruling, **≥1,024 bytes**
headroom) · `current.json` **2,048** · history index **32,768**. Raising a budget is an
explicit control-plane migration decision, never a side effect of a milestone.

**Never delete a historical negative result, and never rewrite a failed experiment as though
it did not happen.** Mark superseded statements as superseded and name what superseded them;
a milestone improving an INSTRUMENT corrects CURRENT-STATE prose only.
