# JARVIS V69 — M62 CURRENT CONTROL PLANE

> **This file is CURRENT STATE ONLY. It is not the event log.**
> Everything M62 has ever done through **S3N** is preserved byte-for-byte in the immutable
> archive below. This file describes only what is true *now*, and it is deliberately small
> enough to read at the start of every session.

| | |
|---|---|
| **Control plane** | V2 · schema `m62.control_plane.1` · state generation **4** |
| **Current state (machine-readable)** | `state/m62/current.json` |
| **Latest snapshot** | `state/m62/snapshots/0004-m62-eval-ceremony-qualified.json` |
| **Snapshot SHA256** | `f563df9b6cfccc6969ca8685b666f72f5739146aafe834655b7e75c4af5f039c` |
| **Subject state commit** | `4f683f788b02c17b043341aa642fb7de68b41ab3` (S3Q.0 ceremony qualification) |
| **Portable training receipts** | `state/m62/receipts/` — tracked, root-independent proof a candidate trained |
| **Historical archive** | `jarvis/docs/m62/history/PROGRESS_THROUGH_S3N.md` |
| **Archive SHA256** | `e0914054da4dde4b785bbdabc45a40e0f8b590c2aa3612e9432c685c0c79c1bf` |
| **History index** | `jarvis/docs/m62/HISTORY_INDEX.md` |
| **Verify first** | `python jarvis/scripts/verify_m62_control_plane.py` |

---

## 0 — Verify before you act

```
git fetch origin --prune && git status -sb
python jarvis/scripts/verify_m62_control_plane.py
```

Expected ending:

```
M62_CONTROL_PLANE_VERIFY:
PASS
PROBLEMS:
0
```

The verifier is offline, read-only, deterministic and fail-closed. It loads no model, no
tokenizer and no weights, writes nothing, and opens no socket. It does not trust this
file: it re-derives the policy digests from the production classes, re-hashes the archive
and the snapshot, and asks Git — not prose — about the branch, the ancestry and `master`.

**If it fails, stop.** See §14.

---

## 1 — Checkpoint

| Field | Value |
|---|---|
| Repository | `alejandrobalsas26-sys/jarvis` (`origin`, HTTPS) |
| Branch | `jarvis-v69-m62-training-gym` |
| Subject state commit | `4f683f788b02c17b043341aa642fb7de68b41ab3` — the commit the current snapshot describes |
| Training source commit | `bac49c4a49194d84fbc7f61656662fdcd54799ca` — the commit candidate 003 actually trained from. **Deliberately different from the subject commit** |
| HEAD | a descendant of the subject commit; resolve with `git rev-parse HEAD` |
| Divergence from origin | `0  0` |
| `origin/master` | `3705114228edef2f665be349c5c4429b7b16777a` — **untouched by M62** |
| Merge / tag / release / version bump | **none** — `core/version.py` still declares `MILESTONE = 61`, deliberately |

**Every hash in this file is a current identity, not a restart target.** Start from current
HEAD. Do not reset to an earlier M62 checkpoint.

**Control-plane commit vs subject-state commit.** The snapshot describes the repository at
the *subject* commit. S3N.1 itself added only control-plane and documentation files on top
of it, which is why the two differ and why the verifier requires HEAD to *descend* from the
subject rather than equal it.

---

## 2 — Current milestone status

| | |
|---|---|
| Milestone | **V69 M62 — Training Gym** |
| Last state-bearing milestone | **S3Q.0** — the one-shot evaluation ceremony qualified and hardened |
| Last milestone | **S3Q.0** — `V69_M62_S3Q0_EVAL_CEREMONY_QUALIFICATION.md`. **PASS.** Six ceremony defects reproduced and closed; no model, no holdout, no authority |
| Phase | **Between milestones.** The student has been taught, the airlock is ready, and the exam is still sealed |
| Live training since S3N | **one run** — candidate 003, 40/40 optimizer steps, `TRAIN` capability spent. **No retry is authorised** |
| Live evaluation since S3N | none — **0 generations, 0 response tokens, 0 held-out reads, no `EVAL` authority** |
| S3Q.0 | infrastructure only — **0 weights loaded, 0 generations, no confirmation string, no plan consumed, `eval-v4` untouched** |

**What M62 is.** The Training Gym: an offline-first, human-gated pipeline that collects and
grades defensive task episodes, builds immutable leakage-checked datasets, plans and
executes a bounded LoRA fine-tune under a single-use token, and runs a paired
baseline-versus-adapter evaluation over a held-out corpus ending in a *non-effectful*
candidate proposal. It has been exercised live end to end, twice, on real weights.
**Nothing has been promoted.**

**Two candidates have been measured and both are `EVALUATED_NOT_ELIGIBLE`, in opposite
directions on the same axis.** Candidate 001 bought required refusal and paid in
over-refusal; candidate 002 repaired over-refusal outright and lost every required refusal,
failing three of nine security vetoes. Structured output sat at exactly 7/9 for both against
a perfect 9/9 baseline — diagnosed in S3M as a **termination** failure, not a formatting one.
**No midpoint between the two is demonstrated.** No ablation was run and none may be run
against a spent holdout.

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

The S3E.2 evaluation load was observed at **bfloat16** because the backend did not force
FP32. That is history and is not rewritten. `tokenizer_chat_template_hash` digests the
template **source**, not the call — read `chat_render_policy_hash` for the call (D37).

---

## 4 — Candidates

| Candidate | State | Adapter SHA256 | Trained on | Measured on | Deep authority |
|---|---|---|---|---|---|
| `qwen3-06b-lora-quality-live-001` | `EVALUATED_NOT_ELIGIBLE` | `43213035…e22ac858` | `train v1` | `eval v2` | `V69_M62_S3I_LIVE_QUALITY_HELDOUT_EVALUATION.md` |
| `qwen3-06b-lora-quality-live-002` | `EVALUATED_NOT_ELIGIBLE` | `319c2524…f9665409` | `train v2` | `eval v3` | `V69_M62_S3L_SECOND_QUALITY_HELDOUT_EVALUATION.md` |
| `qwen3-06b-lora-quality-live-003` | **`TRAINED_UNEVALUATED`** | `6ccd8fdc…c76ce4ea6` | `train v2` | **not yet measured** | `V69_M62_S3P_CANDIDATE003_LIVE_TRAINING.md` |

**Candidate 003 is TRAINED, not measured.** S3O designed it moving **exactly one** primary
axis — training render reasoning policy `MODEL_DEFAULT` → `DISABLED` — with LoRA scope
`ATTENTION_AND_MLP` and `train v2` unchanged. S3P spent its one authorised `TRAIN`
capability: created once, consumed once, one process, **40/40 optimizer steps at exactly
2.0 epochs**, `train_loss` 3.408492, two diagnostic validations plus a closing pass
(3.250574 → 3.178661), one verified 392-tensor LoRA adapter structurally identical to
candidate 002's and byte-different from it. **It is the first M62 candidate ever fitted
under the D37 representation.** Its behaviour is **unknown, not estimated**: no gate has
been evaluated, no grader has run and no response token has been generated.

**The trained claim is not this table's to make.** `check_training_receipt` re-derives it
from the tracked, root-independent receipt
`state/m62/receipts/qwen3-06b-lora-quality-live-003.train.json`, and refuses a snapshot
that agrees with a verifier constant while the receipt is absent or disagrees. The receipt
also means the training history survives a fresh clone that has **no** runtime adapter.
`config_hash` and `plan_hash` are root-bound: re-derive them, never paste them.

**No retry exists.** The capability is spent whatever happens next; a change to candidate
003 is a new identity, never a patch of this one.

**Closed candidate-state vocabulary.** `NOT_CREATED` · `DESIGNED_UNTRAINED` ·
`TRAINED_UNEVALUATED` · `EVALUATED_NOT_ELIGIBLE` · `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` ·
`EVALUATED_NEEDS_MORE_EVIDENCE` · `EVALUATED_QUARANTINED` · `PROMOTED`. Anything else is
refused. The allowed-transition table lives in `jarvis/scripts/verify_m62_control_plane.py`
and the verifier rejects every jump absent from it — including
`NOT_CREATED → PROMOTED`, `TRAINED_UNEVALUATED → PROMOTED` and
`EVALUATED_NOT_ELIGIBLE → PROMOTED`. `EVALUATED_NOT_ELIGIBLE` is **terminal**: a third
candidate is a new identity, never a patch of an old one.

**`PROMOTED` is refused outright in a snapshot.** No promotion mechanism exists in this
repository — `ModelCandidateProposal` is non-effectful by construction — so no artefact
could witness the claim.

---

## 5 — Datasets

| Dataset | Role | State | Manifest | Parent | Pack | Spent by |
|---|---|---|---|---|---|---|
| `m62-defensive-eval v1` | holdout | `USED_IMMUTABLE` | `0970600c…` | genesis | `d714d89b…` | S3E.2 |
| `m62-defensive-eval v2` | holdout | `USED_IMMUTABLE` | `82b60bfd…` | `0970600c…` | `3744a22e…` | S3I LIVE |
| `m62-defensive-eval v3` | holdout | `USED_IMMUTABLE` | `7c948236…` | `82b60bfd…` | `28d2f7d0…` | S3L |
| **`m62-defensive-eval v4`** | holdout | **`FROZEN_UNUSED`** | `8c6871b0…` | `7c948236…` | `95b4e2f6…` | **nothing** |
| `m62-defensive-quality-train v1` | training | `USED_IMMUTABLE` | `9bbac2f0…` | genesis | — | S3H |
| `m62-defensive-quality-train v2` | training | `USED_IMMUTABLE` | `24ceb1e0…` | `9bbac2f0…` | — | S3K |

Full 64-character digests are in the snapshot. Every holdout is 36 tasks, splits 12/12/12,
families 12/9/9/6, decision classes 12/6/18 — the frozen contract, cell for cell.

**`FROZEN_UNUSED` vs `USED_IMMUTABLE` is a scientific property, not a label.**
`FROZEN_UNUSED` means **no model has ever read it**. `USED_IMMUTABLE` means it is spent and
its results are design input from then on (**D35**). The transition is **one-way** and the
verifier has no edge back: relabelling a spent holdout as fresh is the single most damaging
edit anyone could make to this state. A missing or unrecognised status is a **failure**, not
a fresh corpus. (The S3J/S3K spelling `FROZEN_UNSEEN` means the same as `FROZEN_UNUSED` and
survives only in the archive; it is not a third state.)

### `eval-v4` — body-free authority

This is everything a candidate-design session may consume. **Do not read `v4` task bodies.**

```
dataset_id / version    m62-defensive-eval / v4
status                  FROZEN_UNUSED
manifest                8c6871b0094bdfc75062a6352d383fa8e9750c1425182a2b3248db20500081c5
parent                  7c948236163198b5de451316e39346a37efcbc1254724f921e116a6c722f75a0
pack                    95b4e2f6ffb495735113c236f051073449f4562b780eddfc5fe8a7f76bddf2b7
task-hash set digest    959f28f5b37d1bcc53934a0b5be3055c3b2ce1a4192cd5ae5ec2dc05491f9c68
prompt-hash set digest  26493db629d20973acb6333455d3a3af5f268d98f96de0f2ed2a571cbdbfb11e
target-hash set digest  916e1ad9a6f41ff3cd4a1719b536036687ce2fc0a94acf2d2900430ecc53c696
tasks / splits          36 / 12-12-12
families                safety_refusal 12 · structured_report 9 · evidence_request 9 ·
                        tool_call_schema 6
decision classes        required_refusal 12 · required_completion 6 · completion 18
prior overlap           0 on ids, prompts, targets, task/prompt/target/candidate hashes
                        against v1, v2 AND v3
training leakage        CLEAN, 0 findings, against train v1 AND train v2
semantic leakage        NOT_QUALIFIED — never reported clean
```

**The authoritative list of what a candidate-003 session may and may not read is
`jarvis/docs/V69_M62_S3N_FRESH_EVAL_V4_FREEZE.md` §17.** That document contains no task
body and is safe to read in full.

---

## 5b — The evaluation ceremony (S3Q.0)

Qualified and hardened **before** `eval-v4` is spent. Deep authority:
`jarvis/docs/V69_M62_S3Q0_EVAL_CEREMONY_QUALIFICATION.md`.

**Four events, four different facts**, recorded separately because they fail separately:
`PLAN_CONSUMED` · `HOLDOUT_MODEL_FACING_COMMITTED` · `EVALUATION_COMPLETED` ·
`TERMINAL_LEDGER_RECORDED`. Collapsing any pair re-spends a holdout or reports a lost
record as success.

**PROSPECTIVE SPEND RULE.** A fresh holdout is `USED_IMMUTABLE` the moment the evaluator
**durably commits** the first held-out request to the model-facing boundary — after
request parity, immediately before the first `backend.generate`. Ledger event
`holdout_model_facing_committed` (`m62.evaluation_holdout_commit.1`): body-free, unique per
`(evaluation_id, generation, plan_hash)`, fail-closed. **Once written the holdout is spent
whatever happens next, and RERUN is FORBIDDEN.** Prospective only — 001/002 stay sealed.

**EXACT PLAN BINDING.** `task_pack_hash`, `hidden_target_store_hash` and
`order_assignment_hash` were **proxies** (manifest+counts, manifest digest,
`"<policy>:<seed>"`). They are now the runtime identities themselves, derived once by
`training_gym/evaluation/preflight.py` and consumed by planning *and* execution, which
re-verifies them plus both references and the generation policy immediately before the commit
— a mismatch stops the run with the holdout unread. An unblocked plan whose three identities
are not real digests is refused at construction. **`performs_inference` is now TRUE**; it was
`false` on the exact object `--execute` hands to a function loading weights.

**PRE-GO vs GO.** `--live-preflight` prints the exact plan identity and its blockers
**without** materialising `EVAL:<plan-hash>`; `--dry-run` and `--print-plan` **do** emit it.
Token silence is **hygiene, not cryptography** — the string is a pure function of the
`plan_hash` the preflight prints.

**DURABILITY.** `ExecutionOutcome.ok` now requires `COMPLETED` **and** the holdout commit
**and** the terminal ledger line **and** no durability-critical problem. A completed
measurement whose terminal line failed returns `EXIT_DURABILITY` (22): artefacts retained,
plan spent, **recovery required, never a rerun**. A plan-ledger failure is structured, the
plan stays unspent, and the empty directory that attempt created is withdrawn (`rmdir` only).

**PORTABLE EVALUATION RECEIPT.** `scripts/build_m62_eval_receipt.py`, schema
`m62.eval_receipt.1` — deterministic, body-free, binding the three durable events and the
exact identities. **A future `EVALUATED_*` state for candidate 003 REQUIRES one**; snapshot,
verifier constant and prose are insufficient in **both** directions. 001/002 are a **closed**
legacy exemption. **A receipt is evidence, never authority for another operation.**

**Body boundaries.** `ORCHESTRATOR_SEMANTIC_ACCESS` forbidden · `BODY_OPAQUE_PROGRAMMATIC_ACCESS`
permitted for reviewed hashing/validation code · `MODEL_FACING_ACCESS` is the spend.
`task-pack.jsonl` is **`BODY_BEARING`** by design; every other artefact, every ledger line and
the receipt are **`BODY_FREE`**, proved with canaries.

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
auto-safe device and precision). `c6b0b682…` is the **configured** policy the sealed S3I and
S3L config documents declare (`timeout_s` 300, `seed` 11, `cpu`, `fp32`). Both are correct;
they are different objects, and the verifier requires them to differ.

`config_hash` and `plan_hash` bind `output_root_id`, runtime and hardware evidence. **They
are root-dependent: re-derive them on the executing host, never paste a recorded value in.**

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

### Limitations that travel into any successor run

- Gate thresholds are **uncalibrated** (`thresholds_are_calibrated: false`).
- Every holdout is **36 synthetic tasks, one author, one session, no independent review**;
  `tool_call_schema` has only 6.
- **Semantic leakage has never run.** All freshness and leakage evidence is exact or
  lexical; a pure paraphrase would not be caught.
- Candidate 001 was measured on `v2` and 002 on `v3`, with **zero shared task instances**.
  The two tables are **not** a head-to-head ranking. What is comparable is each candidate
  against its **own** simultaneously-measured baseline under identical policy digests.
- **A candidate fitted under `DISABLED` is not directly comparable to 001 or 002.** Binding
  the policy is itself an experimental axis.
- **The D37 fix has been exercised by exactly one live training run** (candidate 003).
  Whether it changes any *measured* behaviour is still **unknown, not estimated** —
  nothing has been evaluated.
- **Candidate 003 is trained and unevaluated.** Its 12-row train-time validation is
  train-side steering material, appears in no gate, and is **not** comparable with
  candidate 001's or 002's numbers. Its adapter has never been loaded for inference.
- **S3P is one host, one seed, one run**, no repeat and no ablation;
  `deterministic_reproduction_claimed` is `false`.
- **No model has read `eval-v4`.** Training candidate 003 did **not** spend it. What it
  will measure is **unknown, not estimated**.
- No model has been generated under the D38 instrument.
- Neither Kali runtime is claimed bytewise equivalent to the Windows runtime that produced
  candidate 001's adapter.
- `openai` is a declared base dependency absent from the system interpreter; its absence
  alone fails 62 tests in three files. Environmental, reproduced at pristine HEAD.
  **Never reconcile test counts across interpreters by arithmetic.**
- Measurement is **one host, CPU, one seed, one run per candidate**.

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
invariant, so the observation is *measured* as "no tracked file carries a token literal" —
the verifier scans for one on every run — and it is **not** proof that none exists
elsewhere. Absence of evidence is not a clean measurement; that is the D38 lesson written
into the schema.

TRAIN, EVAL, promotion, registry mutation and release remain governed **exclusively** by the
existing single-use plan-token mechanism plus an explicit human operator decision. S3N.1
neither moved, replaced nor weakened that mechanism.

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
result          3623 passed · 20 skipped · 0 failed        [S3Q.0]
```

S3Q.0 added **182** tests across seven focused suites (plan truthfulness, exact binding,
holdout commit, ledger durability, body blindness, portable receipt, control-plane
anti-circularity) plus `tests/_s3q0_synthetic.py`. 3441 + 182 = 3623 on one interpreter;
**never reconcile counts across interpreters by arithmetic.**

**Two pre-existing assertions were rescoped, and neither is a regression.** Both compared a
sealed milestone's property against *live* state, so each also asserted that no later
generation existed — true by coincidence until S3Q.0 wrote one. The S3N source-scope test is
pinned to S3N's closing commit `ec446e3`; the S3P gen2→gen3 transition test reads generation
3, not the newest snapshot. Each still measures the property its name states.

**Known invocation-context artefact — do not rediscover it as a regression.** Running
`pytest` from the **repository root** instead of `jarvis/` fails **8** tests in
`jarvis/tests/test_training_gym_m62_s3g2_validation_wiring.py`. S3N reproduced the same 8 at
the pre-S3N commit, and S3N.1 measured the cause: those tests read production source by a
path relative to the working directory, so they resolve only from `jarvis/`. The file is
unchanged since the subject commit.

**It is distinct from D39.** D39 fails **4** tests in the *dataset-exports* file when the
validation-wiring file is collected first. Different file, different count, different cause.
Do not conflate them, and do not fix either as a rider.

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

**Do NOT read by default:**

- the historical archive `PROGRESS_THROUGH_S3N.md` — it is 516,784 bytes and it is history;
- every milestone document;
- `eval-v4` task bodies, in any form, for any reason connected to candidate 003;
- the bodies of any spent holdout;
- raw model responses — none were ever persisted, only `response_sha256`.

**Read history only when** a referenced invariant cannot be resolved from current authority.

**Every high-impact session begins:** Git authority → control-plane verifier → current
state → the milestone document NEXT names → task-specific authority → *only then* writes.

---

## 12 — EXACT NEXT

> **An explicit HUMAN OPERATOR DECISION: whether to grant a separate, single-use `EVAL`
> authority for candidate 003 against `eval-v4`.** Nothing is authorised by S3P. It
> trained a candidate; it measured nothing, generated nothing, created no `EVAL`
> capability and predicted no result.

**The decision is not implied by the candidate being trained.** Spending `eval-v4` is
irreversible: it becomes `USED_IMMUTABLE` the moment a model reads it, and a fourth
candidate would then need a fifth holdout (**D35**). "Candidate 003 exists" and
"candidate 003 should be measured now" are different sentences.

**What is already fixed and must not be reopened:** the candidate identity
`qwen3-06b-lora-quality-live-003`; its adapter `6ccd8fdc…`; the single axis
`MODEL_DEFAULT` → `DISABLED`; LoRA scope `ATTENTION_AND_MLP`; `train v2` unchanged. **The
one authorised training attempt is spent** — no second `TRAIN` token, no retry, no resume,
no re-seed. A change to candidate 003 is a **new identity**.

**If evaluation is later authorised**, the sequence is: derive the evaluation plan on the
executing host in `.venv-m62-eval-linux` → **run `--live-preflight` and review it** (it
prints the exact bound identities and **no** token) → a fresh single-use `EVAL` token → one
paired run against `eval-v4` → **build and verify the portable evaluation receipt** → a new
control-plane generation. The only valid primary comparison is a **simultaneously measured
baseline on `v4`** versus candidate 003 on `v4`, under identical generation, metric and gate
policy digests. D28, D29 and D33 must be restated in that report.

**The ceremony is qualified (S3Q.0) and it is not authorised.** Preparing the airlock is not
opening the exam. `eval-v4` is spent the moment the durable model-facing commit lands, and
after that there is **no rerun** — not for a crash, a failed write or a lost ledger line.

**Comparability, before anyone builds a table.** Candidate 003 is **not** head-to-head
comparable with 001 or 002: it is fitted under a different training representation *and*
will be measured on a different exam. The only valid comparison is candidate 003 against a
**simultaneously measured baseline on `v4`** under identical policy digests.

**Nothing predicts the outcome.** D37's historical causality is `NOT_ESTABLISHED`. Do not
record, anywhere, that candidate 003 will fix stopping, restore 9/9 or repair safety.

**Still explicitly ruled out:** a second `TRAIN` capability for candidate 003 · retraining,
resuming or re-seeding it · reading `eval-v4` bodies to explain, debug or tune it · turning
a `v4` failure into a training example · reporting the 12-row validation curve as a quality
or eligibility result · ranking 001, 002 and 003 in one table · a second experimental axis ·
`ATTENTION_ONLY` · any LR, epoch, rank, alpha or dropout change · `train-v3`, added rows or
a rebalance · a teacher-generated corpus · raising `max_new_tokens` · adding structured rows ·
strengthening the response schema · changing gates, graders, thresholds or the refusal
detector · creating a D38 gate · fixing **D39** as a rider.

---

## 13 — History

| | |
|---|---|
| **Immutable archive** | `jarvis/docs/m62/history/PROGRESS_THROUGH_S3N.md` |
| SHA256 | `e0914054da4dde4b785bbdabc45a40e0f8b590c2aa3612e9432c685c0c79c1bf` |
| Size | 516,784 bytes · 6,089 lines |
| **Router** | `jarvis/docs/m62/HISTORY_INDEX.md` |
| **Migration record** | `jarvis/docs/V69_M62_S3N1_CONTROL_PLANE_V2_ZERO_TRUST_MIGRATION.md` |

The archive is the **byte-for-byte** pre-migration `PROGRESS.md`. It is **append-never-edit**
and its digest is pinned in the snapshot *and* the migration manifest; the verifier compares
both against the bytes.

**If the archive's digest ever fails, the remediation is operator review — never "update the
expected hash".**

Use the history index to find a milestone, a defect or an era **without reading the archive
in full**. Deep milestone documents remain the deep authority and are unchanged.

---

## 14 — Emergency recovery

If `verify_m62_control_plane.py` fails:

**Allowed immediately** — all read-only:

- read the verifier's problem list;
- compare the archive and the snapshot against Git history
  (`git log --oneline -- state/m62 PROGRESS.md`, `git show <commit>:<path>`);
- inspect the snapshot chain by hand;
- read `V69_M62_S3N1_CONTROL_PLANE_V2_ZERO_TRUST_MIGRATION.md`.

**Forbidden without an explicit operator-authorised CONTROL-PLANE RECOVERY milestone:**

- rewriting state, regenerating the archive or updating an expected hash;
- deleting or editing a failing snapshot;
- skipping, weakening or "fixing" the verifier to make it pass;
- continuing with TRAIN, EVAL, promotion, merge, tag or release.

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
milestone report**. Deep detail goes into a milestone document; the history index gains one
row.

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

**Size budgets, enforced by the verifier.** This file: **760 lines / 40,960 bytes** — it
migrated at roughly 510 lines, so there is about 250 lines of headroom, or a dozen normal
milestone closes. The snapshot: **32,768 bytes**. `current.json`: **2,048 bytes**. The
history index: **32,768 bytes**. Exact migrated sizes are in
`state/m62/migrations/0001-control-plane-v2.json`. Raising a budget is an explicit
control-plane migration decision, never a side effect of a normal milestone.

**Never delete a historical negative result, and never rewrite a failed experiment as though
it did not happen.** Mark superseded statements as superseded and name what superseded them.
