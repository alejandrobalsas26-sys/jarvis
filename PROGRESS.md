# JARVIS V69 — M62 CURRENT CONTROL PLANE

> **This file is CURRENT STATE ONLY. It is not the event log.**
> Everything M62 has ever done through **S3N** is preserved byte-for-byte in the immutable
> archive below. This file describes only what is true *now*, and it is deliberately small
> enough to read at the start of every session.

| | |
|---|---|
| **Control plane** | **V4** · schema `m62.control_plane.4` · state generation **35** |
| **Current state (machine-readable)** | `state/m62/current.json` |
| **Latest snapshot** | `state/m62/snapshots/0035-s5g1-preintegration-hardening.json` |
| **Snapshot SHA256** | `60660e492b0d0573b1ff55b5bca13fed6277b2ac0dba38d7f8a814c39c0dc526` |
| **Subject state commit** | `7dda0f50ca0e9e6ffd7ef88dda953b1855b1a308` (S5G.1 pre-integration hardening; eval-v7 still spent once, 005 not eligible, 004 still held) |
| **Integration authority** | base `3705114228edef2f665be349c5c4429b7b16777a` → `refs/heads/master`, **FAST_FORWARD_ONLY**. The observation is DERIVED per run and deliberately not recorded here |
| **Governed checker** | `jarvis/scripts/verify_m62_control_plane.py` sealed at `e70092c79ccd9997d07bc226f349331a2e133e185b3986ba3b0adef37bcf1d05` (S5G.1). Changing it needs a **successor generation**, never a trailing commit |
| **Receipts & records** | `state/m62/receipts/` (portable training/eval proof) · `state/m62/records/` (content-addressed immutable blocks) |
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

The verifier is offline, read-only, deterministic and fail-closed: no model, tokenizer or
weights, no write, no socket. It does not trust this file — it re-derives policy and
instrument identities from the production classes, re-hashes the archive and snapshot, and
asks Git, not prose, about branch, ancestry and `master`.

**If it fails, stop** (§14).

---

## 1 — Checkpoint

| Field | Value |
|---|---|
| Repository | `alejandrobalsas26-sys/jarvis` (`origin`, HTTPS) |
| Branch | `jarvis-v69-s5g-control-plane-v4-integration-semantics` — gen 34 records it as **provenance**; under V4 the branch NAME is not authority (§2) |
| Governed subject | `7d1a0216bb2d55238bc54501f2820023c1195245` — the commit gen 34 describes AND the commit its integration authority governs |
| Training source commits | 003 `bac49c4a…` · 004 `80565d32…` · 005 `08a7e81f157184389ef14d54007478076314c434`. **Deliberately different from the subject commit** |
| HEAD | a descendant of the subject commit; resolve with `git rev-parse HEAD` |
| Divergence from origin | `0  0` |
| `origin/master` | `3705114228edef2f665be349c5c4429b7b16777a` — **untouched by M62**, and now also the **integration base** gen 34 authorises a fast-forward FROM |
| Merge / tag / release / version bump | **none** — `core/version.py` still declares `MILESTONE = 61`, deliberately |

**Every hash here is a current identity, not a restart target.** Start from current HEAD; do
not reset to an earlier M62 checkpoint. **Control-plane commit vs subject-state commit:** the
snapshot describes the repository at the *subject* commit, and a milestone's phase-B commit
adds only control-plane and documentation files on top — which is why the two differ, and why
the verifier requires HEAD to *descend* from the subject rather than equal it. **That gap is
also why V4 exists:** a record can only ever name commits that already exist, so it can never
name the commit that carries it (§2).

---

## 2 — Current milestone status

| | |
|---|---|
| Milestone | **V69 M62 S4H — future evaluation instrument hardening** (M64.1 runtime is frozen infrastructure here and was not touched) |
| Last state-bearing milestone | **S4E** — one paired attempt on `eval-v7` under ONE human `EVAL` authority (plan `54488fb3…`): 36+36 generations, ONE spend, terminal `completed` |
| Last state-bearing M62 | **S4H** — gen 28 `def4b272…`. **FUTURE instruments only** (D45–D48, §7): 005 **not rescored**, `eval-v7` **not reopened**, **0** spends. `…S4H_INSTRUMENT_HARDENING.md` |
| Earlier milestones | **M65A · M65B · M65C · S5E · S5F — RUNTIME or REPOSITORY, no science** (gens 29–33, GOVERNANCE-ONLY). **Universal exactly-once is NOT claimed**: 23 of 24 reachable effectful tools are `NON_REPLAYABLE` (M65C). S5E found CI's authoritative job **exiting 2 and running zero tests**. All five moved **0** spends. Rows verbatim: `jarvis/docs/m62/history/PROGRESS_MILESTONE_ROWS_THROUGH_S5F.md` |
| Earlier milestone | **S5G — CONTROL PLANE V4, no science.** Gen 34 GOVERNANCE-ONLY. V3's `master_commit` had to equal a live ref, so committing a generation onto master invalidated it — a **self-reference in the schema**. V4 declares only commits that **already exist** and **DERIVES** the observation. **0** spends. `…V69_S5G_CONTROL_PLANE_V4_INTEGRATION_SEMANTICS.md` |
| Last milestone | **S5G.1 — PRE-INTEGRATION HARDENING, no science.** Gen 35 GOVERNANCE-ONLY. Three findings, each **reproduced before repair**: (a) three tests pinned the *pre-integration* observation, so an authorised FF turned master CI **red**; (b) the checker sat on its **own** trailing surface — one deleted dispatch line certified an ungoverned runtime module, `PROBLEMS: 0`, **full suite green**; (c) a trailing `tests/conftest.py` turned *3 failed, exit 1* into *3 deselected, exit 0*. Now **executable authority changes only inside a governed subject**; an unclaimed category reports **NOT_RUN**. **0** spends. `…V69_S5G1_PREINTEGRATION_HARDENING.md` |
| Phase | **MEASURED, NOT ELIGIBLE, NO EXAM LEFT.** 001–003 and **005** `EVALUATED_NOT_ELIGIBLE`; **004 stays `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` under its HOLD, not promoted**; `eval-v4`, `v6`, `v7` `USED_IMMUTABLE`; `eval-v5` frozen and retired |
| Live training since S3N | **three runs** — candidates 003, 004 and 005, 40/40 optimizer steps each, all three `TRAIN` capabilities spent. **No retry is authorised** |
| Live evaluation since S3N | **three runs** — S3Q (003 × `v4`), S3Y (004 × `v6`), S4E (005 vs 004 × `v7`, Protocol V4). One plan, one holdout commit and one terminal event each; all three `USED_IMMUTABLE`, **no rerun possible** |
| Next | **A separate human governance decision.** The axis is closed and no holdout remains: a further measurement needs a NEW corpus authored by a session that will not run it. **Readiness is never authority**, and **no `TRAIN`, `EVAL` or promotion authority exists in this repository**. Before `master` may move: **GitHub anti-rewrite protection**, which does not exist yet |

**What M62 is.** The Training Gym: an offline-first, human-gated pipeline grading defensive
episodes, building immutable leakage-checked datasets, running a bounded LoRA fine-tune under
a single-use token and a paired evaluation over a held-out corpus — base-vs-adapter, or since
S4D adapter-vs-adapter — ending in a *non-effectful* proposal.

**Candidates 001–003 and 005 are `EVALUATED_NOT_ELIGIBLE`, each failing differently; 004
cleared every gate and is HELD.** Per-candidate detail, and why each failed, in §4.

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
security outright**, is blocked by one deterministic quality gate (schema validity 9/9 →
**8/9**), and its mean delta does **not** exclude a regression. **`EVALUATED_NOT_ELIGIBLE` is
terminal.**

**Candidate 004 is MEASURED, ELIGIBLE and HELD.** S3U built it from 003's configuration with
**one dial moved** — `learning_rate` 1e-4 → 5e-5 — on an explicit human ruling, that candidate
only. S3V spent **one** `TRAIN` authority: 40/40 steps, one verified 392-tensor adapter. S3Y
spent `eval-v6` **once**; every gate passed, security regressions **0**, operator decision
**HOLD**. **Not promoted**, **no retry**; the `eval-v7` REFERENCE role changes none of it.

**Candidate 005 is MEASURED and NOT ELIGIBLE — on a SECURITY VETO.** S4B built it from 004's
configuration with **one dial moved** — `learning_rate` 5e-5 → 2.5e-5 — on ruling
**S4B-001** (§12); the measured semantic diff is exactly `{learning_rate}`. S4C spent one
`TRAIN` authority; S4E spent `eval-v7` **once** under Protocol V4, 004 as REFERENCE. Three
statements, and collapsing any two is wrong. **(A)** It **won on quality**: mean paired delta
**+0.1714**, 95% CI **[+0.0566, +0.3122]**, 6 refusal failures fixed (critical safety
violations 11 → 5). **(B)** It introduced **1 new secret leak** where the reference produced
**0**. **(C)** The frozen policy vetoes any new security regression whatever the delta says —
`security_is_a_veto_not_a_weight`. So 3 blocking gates, 2 security, **NOT_ELIGIBLE**. The
**median** delta is **+0.0013** — the mean is carried by six pairs flipping 0→1.
`…S4F_CANDIDATE005_…md`.

**A training loss decides nothing**, nor a mean delta. **No exam remains** (§5);
`TRAINING_ROOT_CAUSE_CONFIDENCE` stays **NOT_ESTABLISHED**.

**No claim here is this table's to make.** `check_training_receipt` and
`check_evaluation_receipt` re-derive them from the tracked receipts, refusing a snapshot that
agrees with a verifier constant while a receipt is absent or disagrees; the `EVALUATED_*`
verdict comes from the **production** decision function.

**Closed candidate-state vocabulary.** `NOT_CREATED` · `DESIGNED_UNTRAINED` ·
`TRAINED_UNEVALUATED` · `EVALUATED_NOT_ELIGIBLE` · `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` ·
`EVALUATED_NEEDS_MORE_EVIDENCE` · `EVALUATED_QUARANTINED` · `PROMOTED`. Anything else is
refused, and every transition absent from the verifier's table is rejected — including
`TRAINED_UNEVALUATED → PROMOTED`. **`PROMOTED` is refused outright in a snapshot:** no
promotion mechanism exists, so nothing could witness it.

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

Full digests live in the snapshot records. Every holdout is 36 tasks, splits 12/12/12,
families 12/9/9/6, classes 12/6/18 — the frozen contract, cell for cell.

**`FROZEN_UNUSED` vs `USED_IMMUTABLE` is a scientific property, not a label.**
`FROZEN_UNUSED` means **no model has ever read it**; `USED_IMMUTABLE` means it is spent and its
results are design input from then on (**D35**). The transition is **one-way**, and a missing
status is a **failure**, not a fresh corpus.

### `eval-v7` — SPENT ONCE, the first REFERENCE-ADAPTER exam (S4D froze, S4E spent)

Task / prompt / target set digests `a5bc453a…` · `8226b43a…` · `d9014520…`, body-free.
**Authored candidate-blind.** Freshness against `v1`–`v6`: **0 exact overlaps, 0 WARN, 0
BLOCK** over 1,138 comparisons, comparator non-vacuous. S4E spent it under Protocol V4 — 004
**REFERENCE**, 005 **CANDIDATE**, one attempt, one spend, 72 generations, terminal `completed`.
`USED_IMMUTABLE` is **terminal**: a second spend is refused mechanically.
`…S4D_EVAL_V7_FREEZE.md` · `…S4F_CANDIDATE005_…md`.

### `eval-v6` — SPENT ONCE on candidate 004 · `eval-v5` — RETIRED, never spent

`v6`: `spent_by` S3Y LIVE, candidate 004; parent `e852f462…` **declared, not discovered**
(**D34**); freshness measured **before** the spend, 2,564 comparisons, **0 overlaps**, authored
candidate-blind. **No rerun.** `…S3X1_EVAL_V6_FREEZE.md`.

`v5`: **RETIRED FROM ELIGIBILITY USE at generation 12, still `FROZEN_UNUSED`, `spent_by`
null** — set digests `cda48cf5…` · `239c6402…` · `47dbb2a0…`. Both halves are true and neither
collapses into the other: 0 loads, 0 generations, 0 spends, no receipt, against one
pre-authorisation body exposure (**D44**, §7; rule §8). A later version declaring it an
**ancestor** is lineage, not reuse. `…S3S_EVAL_V5_FREEZE.md`.

**`eval-v4` is spent** (S3Q, candidate 003). **Under D35 it is development evidence and may
never decide eligibility again.** **Every spent holdout's bodies stay unread.**

---

## 5b — The evaluation ceremony

`…S3Y_…` owns 004's measurement and `…S4F_…` owns 005's, with the full body-free results and
receipt derivations (index §13). **Four events, four different facts** (§8): `PLAN_CONSUMED` ·
`HOLDOUT_MODEL_FACING_COMMITTED` · `EVALUATION_COMPLETED` · `TERMINAL_LEDGER_RECORDED` — each
live evaluation recorded **exactly one of each**, under **one** plan hash.

**PROSPECTIVE SPEND RULE** (§8). A holdout is `USED_IMMUTABLE` the moment the evaluator
**durably commits** the first held-out request to the model-facing boundary — after request
parity, immediately before the first `backend.generate`. Once written it is spent whatever
happens next, and **RERUN IS FORBIDDEN**: not for a crash, a failed artefact write, a lost
terminal line or a receipt that will not build. `v4`, `v6` and `v7` crossed it; **`v5` never
did** — hence `FROZEN_UNUSED`, not spent.

**PORTABLE RECEIPTS.** `m62.eval_receipt.3` / `.4` and `m62.train_receipt.1` are
deterministic, body-free, atomic, and the **only** things that may carry an `EVALUATED_*` or
`TRAINED_*` state out of a gitignored runtime tree; eligibility is **re-derived**, never
copied. **THE MEASUREMENT WITNESS** (`state/m62/witnesses/`) is **not a receipt:** it grants no
state, authorises no retry, and establishes **repository provenance, NOT execution
attestation** — nothing signed.

**Body boundaries.** `ORCHESTRATOR_SEMANTIC_ACCESS` forbidden — **enforced in memory, not only
on disk (D44)** · `BODY_OPAQUE_PROGRAMMATIC_ACCESS` permitted for reviewed hashing/validation ·
`MODEL_FACING_ACCESS` is the spend. `task-pack.jsonl` is **`BODY_BEARING`**; everything else is
**`BODY_FREE`**.

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
*alone* hashes to `1b4696d6…` (library defaults); `c6b0b682…` is the **configured** policy
the sealed S3I and S3L configs declare (`timeout_s` 300, `seed` 11, `cpu`, `fp32`). Both are
correct, they are different objects, and the verifier requires them to differ. `config_hash`
and `plan_hash` are **root-dependent — re-derive on the executing host, never paste a
recorded value in.** D46: the recorded `cpu`/`fp32` was never passed to the loader; fixed
prospectively, changing no digest.

---

## 7 — Open defects and current limitations

Only what still binds operation; **D1–D27, D25, D30, D32 and D34 are settled** and live in
the archive and the snapshot's `defects` record, which carries every defect in full. Index:
`jarvis/docs/m62/HISTORY_INDEX.md`.

| Defect | State | What it still binds |
|---|---|---|
| **D28** | **OPEN** | No tool-call transport exists, so `tool_call_validity_rate` is **VACUOUS** on both arms and the six `tool_call_schema` tasks decide nothing. |
| **D29** | ACCEPTED LIMITATION | `looks_like_refusal` reads sixteen literal phrasings absent from the JSON refusal targets. Bounds QG-1 and SV-5 **both ways**. |
| **D33** | **OPEN** | The declared generation timeout is **not enforced**, so `timeout_rate` is **VACUOUS**. A config must state `timeout_s`; default 120 s. |
| **D35** | OPERATOR RULING | A spent holdout is development evidence. **Each candidate needs a fresh holdout.** Not a contamination claim. |
| **D31 · D36 · D37** | FIXED | VALIDATION is wired, **diagnostic only** — do not remove `eval_dataset` or the closing `trainer.evaluate()`. The identity redactor matches unless flanked by ASCII letters both sides — **do not simplify, nor widen to `\b`**. Training binds a reasoning policy; `chat_render_policy_hash` binds the *call*; **causality NOT_ESTABLISHED**. |
| **D38** | FIXED (obs. only) | Output-budget exhaustion is a body-free diagnostic. **No gate reads it; adding one needs a separate operator decision.** |
| **D39** | **FIXED · S5F** | An in-process `importlib.reload` rebound `ExportError`, so four `pytest.raises` sites caught a dead class. **Import purity is a subprocess probe.** `…S5F_…md`. |
| **D40–D42** | FIXED at `.3` | The paired outcome is **not** an exhaustive `wins/ties/losses` partition; an encoding question is closed by **defining** the encoding. |
| **D44** | **FIXED · GATE** | A held-out body reached a session **before any authorisation existed**, through representation alone — `repr` of a **bound method** included. `body_free_repr` renders identity and digests only. |
| **D43 · D45–D47** | FIXED (obs. only) | `EXTRA_DATA` vs an unclosed document; S4H's instrument findings. **PROSPECTIVE**, read by **no gate**; **005 not rescored**. |
| **D48** | **FIXED** | `-k m62` deselected all 212 tests in three `m63`-named modules asserting M62 state. **A filename substring is not a scientific boundary.** `state/m62/scientific-suite.json`. |
| **D49–D54** | **FIXED · S5G.1** | A pinned *pre-integration* observation turned master CI red; the checker sat on its **own** trailing surface; a trailing `tests/conftest.py` deselected `jarvis/tests` failures; **no CI job ran the checker at all** (D52); the digest pin was **resealable beside it** (D53). **Executable authority changes only inside a governed subject, and sealed artefacts are append-only.** `…S5G1_…md`. |

### Limitations that travel into any successor run

- Gate thresholds are **uncalibrated** (`thresholds_are_calibrated: false`). Every holdout,
  `v7` included, is **36 synthetic tasks, one author, one session, no independent review**;
  `tool_call_schema` has 6, vacuous by D28. **Semantic leakage has never run** — freshness
  evidence is lexical only, so a paraphrase would not be caught.
- **Candidates 001–004 are not head-to-head comparable** — different holdouts, zero shared
  instances, one fitted under `DISABLED`. Each compares only against its **own**
  simultaneously-measured baseline. `eval-v7` was the first true head-to-head, it is **spent**,
  and 005-vs-004 is the only such figure that exists.
- **Every candidate was measured ONCE.** One host, CPU, one seed, one run; no repeat, second
  host, GPU, dtype arm or ablation; `deterministic_reproduction_claimed` is `false`. Train-time
  validation is steering material, appears in no gate, is **not** comparable.
- **Candidate 003's interval does not exclude a regression.** Mean delta +0.044208, CI95
  [−0.022359, +0.129413] over 36 pairs; recorded `regression_not_excluded`. **The D37 axis is
  neither confirmed nor refuted**; historical causality stays `NOT_ESTABLISHED`.
- `openai` is a declared base dependency absent from the system interpreter; its absence alone
  fails 62 tests in three files. Environmental, reproduced at pristine HEAD. **Never reconcile
  test counts across interpreters by arithmetic.**
- **The receipt claims less than it appears to.** `evaluation_source` binds the measuring
  commit **through the pre-repair witness and its Git first parent** — provenance, not proof of
  which bytes ran. **Nothing is signed.** **`STALE_STATE` detection remains PARTIAL**: runtime
  artefacts are outside Git.
- **S4H's instruments are FUNCTIONAL, not CALIBRATED** — `REAL_WORLD_CALIBRATED = NO` — and
  **additive and inert**: no config names one, no scorer imports one, nothing was rescored.
- **The D44 exposure is PERMANENT** — no fix restores `v5`'s freshness, and it was **not**
  re-measured: re-opening the material to size it would repeat the disclosure. One rendered
  body is a **floor**, not a proved bound.
- ~~**V4 leaves a hostile docs-or-tests trailing commit to the CI suite**~~ (S5G) —
  **SUPERSEDED BY S5G.1, measured false**: the suite cannot be that control, because a
  trailing `tests/conftest.py` silences it (*3 failed → 3 deselected, exit 0*). Trailing
  surface is now `state/m62/` · `PROGRESS.md` · `jarvis/docs/`, **and `.md` or `.json` only**.
- **The checker cannot prove itself** (S5G.1): `check_verifier_integrity` is IN the checker.
  Closed is the **silent** case — such a lineage is ungoverned and needs a **successor**. A red
  team falsified S5G.1's own first draft twice (**D52, D53**); both are closed and recorded.
  Inside a subject, a `.json`/`.md` a gate READS is still authority as data — **review is the
  control there**, not the closure.
- **EXTERNAL_CONTROL_PENDING.** `master` has **no** protection and **no** ruleset. An
  exact-base rollback is **DETECTED, PREVENTED by nothing**. S5G.1 does not fix this.
- **Nine closure defects have been MEASURED and closed** — four at S5G (wrong lineage,
  `startswith` on a FILE entry, rename-hidden sources, unhashed records) and five at S5G.1
  (D49–D53, two of them against S5G.1's own first draft). **Every one PASSed every gate this
  repository had while it was live**, so "the suite would catch it" is the assumption that has
  now failed nine times. `…S5G_…SEMANTICS.md` §5.1–5.4 · `…S5G1_…md` §5.

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
  dataset lineage is DECLARED, never discovered** (D34).
- **Gate policy `e5003319…` is byte-pinned by test.** QG-2 stays absolute, FG-1/FG-2 stay
  baseline-relative, and 7/9 is not "good enough".
- **ONE WRITER PER CONTROL-PLANE GENERATION.** If the current generation is not the one you
  expected, **stop**: no last-write-wins, no automatic snapshot merge. **If the verifier
  fails, do not write, train, evaluate or promote**, and never repair state automatically.
- **ORCHESTRATOR BODY-BLINDNESS IS A GATE.** A held-out body may not reach an orchestration
  session by **any** route — `repr`, `str`, f-string, `%r`, logging, exception text, traceback,
  `repr` of a bound method. A body-bearing dataclass **must** install a body-free `__repr__`;
  the persistence firewall alone is **not** enough (**D44**).
- **`eval-v5` ELIGIBILITY USE IS RETIRED**, prospectively from generation 12. It stays
  `FROZEN_UNUSED`, `spent_by` null, because **no model ever saw it**.
- **An `EVALUATED_*` state is REDERIVED, never read.** The receipt carries the body-free gate,
  bootstrap, empirical-status and serialisation evidence, and the **production**
  `decide_eligibility` concludes. **One eligibility algorithm**; a receipt that merely *states*
  a verdict evidences nothing.
- **A FUTURE instrument is pinned by exact version, never "latest"**, and no historical scorer
  may import one. Its finding carries a rule id and a class, **never a matched value** (S4H).
- **EXECUTABLE AUTHORITY CHANGES ONLY INSIDE A GOVERNED SUBJECT** (S5G.1) — not the checker,
  not either test tree, not `conftest`, workflows or packaging. Those need a **successor**.
- **SEALED ARTEFACTS ARE APPEND-ONLY** (S5G.1 · D53). A trailing commit may **add** a snapshot
  or record and may move `current.json`; it may never modify or delete a snapshot, record,
  receipt, schema or the archive. A pin editable beside what it pins is not a pin.

---

## 9 — Authority observation

```
train / eval / promotion            NONE_OBSERVED_IN_REPOSITORY
control_plane_can_grant_authority   FALSE
```

**Three `TRAIN` capabilities created and consumed** — S3P (003), S3V (004), S4C (005) — and
two `EVAL`, S3Y (`v6`) and S4E (`v7`). All spent; no reusable capability exists and none may
be minted. **A spent single-use token is not an authority anyone holds.**

**This is an OBSERVATION, never a grant.** Plan tokens live outside the repository by
invariant, so it is *measured* as "no tracked file carries a token literal" and is **not** proof
that none exists elsewhere. TRAIN, EVAL, promotion, registry mutation and release stay governed
**exclusively** by the single-use plan-token mechanism plus an explicit human decision.

### Operations requiring new explicit operator authorisation

| Operation | Why |
|---|---|
| Any live training | a fresh plan and a fresh single-use `TRAIN:` token |
| Any live evaluation | a fresh generation, plan and single-use `EVAL:` token |
| Freezing a fresh holdout (`eval-v8`) | a new session that did not design the candidate, plus an explicit human decision |
| Registry mutation, promotion, activation, role assignment, adapter merge | no authority here grants these |
| Merging M62, tagging, releasing, bumping `core/version.py` | M62 closure is an explicit operator decision |
| Touching the global environment, or any network or model-hub contact at run time | the no-global-mutation and offline-first invariants |
| Changing the checker, either test tree, `conftest`, a workflow or packaging | S5G.1: those are a **governed subject**, never trailing state |
| Moving `master` | a **successor generation** AND repository anti-rewrite protection, which does **not** exist yet |

---

## 10 — Authoritative test baseline

```
CI-authoritative  python -m pytest -q --tb=short jarvis/tests tests    [ci.yml, BLOCKING]
run from          repository ROOT · CPython 3.11.16 · pytest 8.4.2 (constraints-ci)
result            10805 passed · 45 skipped · 0 failed      [S5G.1, measured]
scientific        verify_m62_scientific_suite.py --print-invocation -> pytest <54 modules>
run from          jarvis/ (repository system interpreter)
result            3179 passed · 2 skipped · 0 failed        [S5G.1, re-measured]
```

**S5G.1 also measures it with `master` FAST-FORWARDED onto the tip**, in a disposable clone:
`10768 passed · 82 skipped · 0 failed`, **exit 0**. That run is the F1 proof and is **mandatory
before any integration** — the three rescoped tests PASS there, and before S5G.1 the same three
FAILED. The skip delta is a *clone* property
(gitignored runtime evidence absent), not an integration one.

**The first row is new in S5E, and its absence WAS the milestone.** Counts are **one**
interpreter's; **never reconcile across interpreters.** **`-k m62` is not the authority (D48):**
it deselected all **212** tests in three `m63`-named modules asserting M62 state.
**Rescoped assertions are not regressions** — one comparing a *sealed* milestone's property
against *live* state also, silently, asserts no later generation exists, and S5G.1 found three
asserting **integration has not happened**; each rescoping is argued in its own document.

**Known invocation-context artefact — RESOLVED by S5E, 8 → 0.** `pytest` from the
**repository root** instead of `jarvis/` used to fail **8** tests in
`…s3g2_validation_wiring.py`, which read production source relative to the CWD; it is
**distinct from D39**. The repository root **is** the authoritative invocation. Now anchored to
`_APP_ROOT`.

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
and being spent is not permission; `v5` above all, retired and unread, as is the S3W.1 material
that exposed one** · raw model responses, none persisted. **Read history only when** a
referenced invariant cannot be resolved from current authority.

---

## 12 — EXACT NEXT

> **A SEPARATE HUMAN GOVERNANCE DECISION.** Candidate 005 is measured and **NOT ELIGIBLE**
> on a security veto. `eval-v7` is spent and immutable, and **no holdout remains**. Nothing
> model-facing is authorised: no re-run, no promotion, no candidate 006, no `eval-v8`.
> **`TRAIN`, `EVAL` and promotion authority are all NONE.** S4H hardened the instruments a
> future run would use and **measured nothing**: readiness is never authority.

**The axis is closed.** A further measurement needs a **new** corpus, authored by a session
that will not run it, plus a fresh single-use authority — neither exists, and a corpus is
readiness, not permission. `eval-v7` may never be reopened; a successor may study the
regression **body-free**.

**What the result decided, and what it did not.** It measured one axis once; it did **not**
make candidate 004 promotable, whose HOLD rests on its own `v6` evidence. `RECOMMENDED_REMEDY`
is still **TOOLING** and the candidate still
**`TRAINING_EXPERIMENTALLY_ALLOWED_NOT_PROVEN_NECESSARY`**; the receipt records
`validation_is_held_out_eligibility_evidence: false`. **The S4B, S4C and S4E authorities are
spent** and single-use tokens are **never replayed** — no second seed, no 005b, no resume, no
retry because a result looks unappealing. Neither learning-rate ruling is general. `…S4C_…md`.

**The HOLD on candidate 004 stands.** Nothing since has reopened, re-measured or
reinterpreted it; serving as the `eval-v7` REFERENCE arm does not either. It remains
`EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW`, not promoted; the only legal move out is `→ PROMOTED`
under a `HUMAN_PROMOTION_AUTHORITY` that does not exist. **No second look:** `v4`, `v6` and
`v7` are `USED_IMMUTABLE` — no rerun, re-score, alternate seed, partial replay, ablation, task
inspection, qualitative review or threshold experimentation — and `v5` is RETIRED unspent,
`spent_by` null. Every spent holdout stays **unread**.

**Master is NOT merged, and the next gate is not code.** S5G.1 closed the code boundary; it
did **not** give `master` an anti-rewrite rule, and the repository has none (0 rulesets,
`protected: false`). **Configure repository protection BEFORE integration is retried.**

**Explicitly NOT authorised** — the snapshot's `next_milestone.ruled_out` is the authority
and is longer: promotion, activation, registry mutation, merge, tag, release or version bump ·
**candidate 006** · recording 005 as eligible · spending `eval-v7` twice or editing it · a
second run, seed or value of the axis · retraining or patching 003, 004 **or 005** ·
any epoch, rank, alpha, dropout or module change · a second axis · `train-v3` · changing gates,
graders, thresholds or the refusal detector · reading `v4`–`v7` bodies · raising a budget ·
**`SYNTHETIC_CALIBRATION` cited as calibration** · **executable trailing state** ·
**rewriting a sealed artefact**.

**`PROSE_CANNOT_GRANT_AUTHORITY`** (§8): neither this file, the snapshot, a ruling, a receipt
nor a milestone document authorises any of the above. **A receipt is evidence of an operation,
never authority for another.**

---

## 13 — History

**Immutable archive** `jarvis/docs/m62/history/PROGRESS_THROUGH_S3N.md` · SHA256
`e0914054da4dde4b785bbdabc45a40e0f8b590c2aa3612e9432c685c0c79c1bf` · 516,784 bytes · 6,089
lines · **Router** `jarvis/docs/m62/HISTORY_INDEX.md` · **Migration record**
`jarvis/docs/V69_M62_S3N1_CONTROL_PLANE_V2_ZERO_TRUST_MIGRATION.md`.

The archive is the **byte-for-byte** pre-migration `PROGRESS.md`, **append-never-edit**, its
digest pinned in the snapshot *and* the migration manifest and compared against the bytes.
**If it fails: operator review, never "update the expected hash".** Use the index to find a milestone,
defect or era **without reading the archive in full**.

---

## 14 — Emergency recovery

If `verify_m62_control_plane.py` fails:

**Allowed immediately, all read-only:** read the problem list · compare archive and snapshot
against Git history (`git log --oneline -- state/m62 PROGRESS.md`) · inspect the chain · read
the migration documents. **`NOT_RUN` is a real outcome (S5G.1)**: a category printing it means
a check never executed, which is never a pass.

**Forbidden without an explicit operator-authorised CONTROL-PLANE RECOVERY milestone:**
rewriting state, regenerating the archive or updating an expected hash · deleting or editing
a failing snapshot · skipping, weakening or "fixing" the verifier to pass · continuing with TRAIN,
EVAL, promotion, merge, tag or release.

**Recovery principle: FAIL CLOSED.** Never repair state automatically; a verifier adjusted
until it passes has verified nothing.

---

## 15 — Update protocol for this file

Control Plane V4 separates five roles that must not collapse into one: **CURRENT control
state** (this file + `current.json` + the latest snapshot) · **DEEP authority** (milestone
documents) · **HISTORICAL event log** (the archive) · **NAVIGATION** (`HISTORY_INDEX.md`) ·
**TRUST BOUNDARY** (`verify_m62_control_plane.py`). **A normal milestone close may update**
the current status, NEXT, the defect and limitation lists, the test baseline and the
READ-FIRST pointers — and **must not append the milestone report**. Deep detail goes to a
milestone document; the index gains a row. **Since S5G.1 the trust boundary is not trailing
state**: the checker changes only inside the governed subject, so the topology is *subject
commit carrying code, tests and the checker* → *governance commit carrying `state/m62/`,
`PROGRESS.md` and `jarvis/docs/` only*.

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
test requiring **150 lines of headroom**, so a close that grows it must **recompact** under
**610 lines**, folding superseded detail into the milestone document that owns it. Snapshot
**34,816 bytes** (migrated from 32,768 at S3X.0 by operator ruling, **≥1,024** headroom) ·
`current.json` **2,048** · history index **32,768**. Raising a budget is an explicit
control-plane migration decision, never a side effect of a milestone.

**Never delete a historical negative result, and never rewrite a failed experiment as though
it did not happen.** Mark superseded statements as superseded and name what superseded them;
a milestone improving an INSTRUMENT corrects CURRENT-STATE prose only.
