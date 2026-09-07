# M62 control plane — the milestone rows PROGRESS.md §2 carried through S5F

**ARCHIVAL. APPEND-NEVER-EDIT. This document grants nothing.**

S5G compacted `PROGRESS.md` §2 losslessly. `PROGRESS.md` is CURRENT state; the deep
authority for each milestone is its own document, and §15 of `PROGRESS.md` says a
milestone close "must not append the milestone report". Three rows had accumulated the
report anyway, one milestone at a time, until the file stood 16 bytes under its cap.

The rows are reproduced here **verbatim**, exactly as they stood at generation 33, so the
compaction removes duplication and no fact. Every one of them is also stated, in more
detail, in the milestone document it names.

---

| Previous milestones | **M65A · M65B — RUNTIME, no science** (gens 29–30, GOVERNANCE-ONLY; `GIT_AUTHORITY` pins the branch). Specialist execution core and model-role routing, then a governed **team** (validated DAG, bounded parallelism, delegation, cancellation, team ARGUS) and a hardened `ToolExecutor` — exactly-once had been **sequential only**. `…M65A_SPECIALIST_EXECUTION_CORE.md` · `…M65B_TEAM_EXECUTION_FABRIC.md` |
| Previous milestone | **M65C — RUNTIME, no science.** Gen 31 GOVERNANCE-ONLY. **Durable effect journal** (SQLite, fail-closed): effect identity, ownership and lifecycle survive a crash, a SIGKILL, a restart; a committed effect is recovered, never re-run; the window where an effect happened and its commit did not is **INDETERMINATE**, never guessed. **Universal exactly-once is NOT claimed**: 23 of 24 reachable effectful tools are `NON_REPLAYABLE`. `…M65C_DURABLE_EFFECT_JOURNAL.md` |
| Last milestone | **S5E — REPOSITORY REALITY, no science.** Gen 32 GOVERNANCE + REPAIR. CI's authoritative job (`pytest jarvis/tests tests` from the repository **ROOT**, 3.11) **exited 2 and ran zero tests** at the M65C seal: a regular `tests` package shadowed the namespace one whatever `sys.path` said. Same root cause 13× more (sources read CWD-relative), a red `ruff` gate (27), a Bandit Low ceiling breached 5 weeks, a consistency checker comparing copies not reality. Fixed, and now **executed** by tests. **0** loads / generations / authorities / spends. `…V69_S5E_REALITY_RECONCILIATION.md` |

---

## Where each of these now lives

| Milestone | Deep authority |
|---|---|
| M65A | `jarvis/docs/V69_M65A_SPECIALIST_EXECUTION_CORE.md` |
| M65B | `jarvis/docs/V69_M65B_TEAM_EXECUTION_FABRIC.md` |
| M65C | `jarvis/docs/V69_M65C_DURABLE_EFFECT_JOURNAL.md` |
| S5E | `jarvis/docs/V69_S5E_REALITY_RECONCILIATION.md` |
| S5F | `jarvis/docs/V69_S5F_D39_ORDER_ISOLATION.md` |
| S5G | `jarvis/docs/V69_S5G_CONTROL_PLANE_V4_INTEGRATION_SEMANTICS.md` |

Nothing here is authority. `PROSE_CANNOT_GRANT_AUTHORITY`.


---

## Passages S5G moved out of `PROGRESS.md`, verbatim

### PROGRESS §5b — PORTABLE RECEIPTS, in full

**PORTABLE RECEIPTS.** `m62.eval_receipt.3` and `m62.train_receipt.1` are deterministic,
body-free, atomic, and the **only** things that may carry an `EVALUATED_*` or `TRAINED_*`
state out of a gitignored runtime tree; eligibility is **re-derived** by production
`decide_eligibility`, never copied (`.2`'s refusals were **D40–D42**, §7).
`m62.eval_receipt.4` is S4D's **additive** reference-adapter shape: **no `baseline` field at
all**, so it cannot record an adapter as a bare base model; no prior receipt is migrated.
**THE MEASUREMENT WITNESS** (`state/m62/witnesses/`) is **not a receipt:** it grants no state,
authorises no retry, promotes nothing, and establishes **repository provenance, NOT execution
attestation** — nothing signed, no PKI implied.

### PROGRESS §10 — the S5E baseline narrative, in full

**The first row is new in S5E, and its absence WAS the milestone.** This section recorded
only suites run from `jarvis/`; the command `ci.yml` calls authoritative runs from the
repository ROOT, and at the M65C seal it exited **2** and ran **zero** tests. Counts are
**one** interpreter's; **never reconcile across interpreters.**

**`-k m62` is no longer the authority (D48):** it matches node ids and deselected all **212**
tests in three `m63`-named modules asserting M62 state.

**Rescoped assertions are not regressions.** An assertion comparing a *sealed* milestone's
property against *live* state also, silently, asserts that no later generation exists. Each
rescoping is argued in its own milestone document.


### PROGRESS §7 — the successor-run limitations, in full as they stood at generation 33

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



### PROGRESS §9 — the authority-observation caveat, in full

**This is an OBSERVATION, never a grant.** Plan tokens live outside the repository by
invariant, so it is *measured* as "no tracked file carries a token literal" — the verifier
scans every run — and is **not** proof that none exists elsewhere. Absence of evidence is not
a clean measurement; that is the D38 lesson. TRAIN, EVAL, promotion, registry mutation and
release stay governed **exclusively** by the single-use plan-token mechanism plus an explicit
human decision, which no milestone since has moved, replaced or weakened.

### PROGRESS §5b — the ceremony ordering, in full

Qualified before `eval-v4` was spent (S3Q.0), chain closed before it (S3Q.0.1), executed
once (S3Q), sealed after (S3Q.0.2); `…S3Y_…` owns 004's measurement and `…S4F_…` owns 005's.
Those documents own the full body-free results and receipt derivations (index §13). **Four
events, four different facts** (§8): `PLAN_CONSUMED` · `HOLDOUT_MODEL_FACING_COMMITTED` ·
`EVALUATION_COMPLETED` · `TERMINAL_LEDGER_RECORDED` — each live evaluation recorded
**exactly one of each**, under **one** plan hash.
