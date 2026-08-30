# V69 M62 — HISTORY INDEX

**A router, not a record.** This file exists so a future session can find the one thing it
needs without reading a 516 KB archive or opening forty milestone documents. It repeats no
milestone content: every row points somewhere.

| | |
|---|---|
| Immutable archive | `jarvis/docs/m62/history/PROGRESS_THROUGH_S3N.md` |
| Archive SHA256 | `e0914054da4dde4b785bbdabc45a40e0f8b590c2aa3612e9432c685c0c79c1bf` |
| Archive size | 516,784 bytes · 6,089 lines |
| Current state | `PROGRESS.md` · `state/m62/current.json` |
| Migration record | `jarvis/docs/V69_M62_S3N1_CONTROL_PLANE_V2_ZERO_TRUST_MIGRATION.md` |

**The archive is append-never-edit.** It is the pre-migration `PROGRESS.md`, byte for byte.
Section numbers below (`§n`) are that document's own headings — search for
`## <n> — ` to land on one.

**Do not read the archive by default.** Open it only when a current authority genuinely
cannot answer the question, and then only the section named here.

---

## Milestones

Every deep document lives in `jarvis/docs/` unless the path says otherwise. Commit ranges
resolve with `git log --oneline <range>`.

| Milestone | Result | Deep authority | Range | Why you would open it |
|---|---|---|---|---|
| **S1** | COMPLETE | archive §4 | `7d1f806`…`c58a69d` | the frozen gym contracts and the sandbox command auditor |
| **S2a** | folded into S3A | archive §4 | `191883f` | why there is no separate S2a commit group |
| **S2b** | COMPLETE | archive §4 | `94b83a9`…`e9273e3` | the eleven deterministic graders; the non-vacuous grader rule (D2) |
| **S2c** | COMPLETE | archive §4 | `96b54b0`…`87f4524` | teacher ensemble, sanitization, human-gated consensus |
| **S2d** | COMPLETE | archive §4 | `b66f157`…`4c8fcb1` | the data factory: candidates → splits → manifests → promotion → exports |
| **S3A** | COMPLETE | `V69_M62_S3A_TRAINING_PLANNER.md` | `191883f`…`b8f7d1c` | the planner, the `TRAIN:` token, and the proof that planning is a dry run |
| **S3B** | COMPLETE | `V69_M62_S3B_TRAINING_EXECUTION.md` | `696dc5b`…`df17d5d` | how a token becomes a run; replay-safe run state; artifact policy |
| **S3C** | COMPLETE | `V69_M62_S3C_ADAPTER_EVALUATION.md` | `4a40ccf`…`e6cf8d0` | paired evaluation, hidden packs, `parity_hash`, the two-backend rule |
| **S3D** | COMPLETE after 3 failures | archive §4 | `1034a78`…`d51ef04` | D11–D15, the transformers-5 compatibility defects |
| **S3D.1** | PASS | `V69_M62_FIRST_LORA_SMOKE.md` | `9a3a370`, `2c35c05` | the first verified real adapter (run-004); D16 checkpoint refusal |
| **S3E** | BLOCKED | `docs/V69_M62_S3E1_LIVE_EVALUATION_INFRASTRUCTURE.md` (top-level `docs/`) | `b9fa160`, `01d73ba` | the unwired `--execute` refusal and the five inherited blockers |
| **S3E.1** | READY | same as above | `aa19eb0`…`57f7a58` | D18–D21; the execution-order guarantees and the 21-state machine |
| **S3E.2** | first real 72-generation measurement | `V69_M62_FIRST_LIVE_ADAPTER_EVALUATION.md` | `dc9763d`…`56d9060` | the run of record for run-004; archive §9–§12 hold its numbers |
| **S3F** | PASS | `V69_M62_S3F_REVIEW_AND_CALIBRATION.md` | `56d9060`…`28f1d45` | **D24 grader saturation** — why `<think>` pinned every score at the floor; D25 |
| **S3F.1** | PASS | `V69_M62_S3F1_STRUCTURED_OUTPUT_AND_REVIEW_EVIDENCE.md` | same range | **D26a/D26b** — the thinking policy and real schema validation |
| **S3F.2** | PASS | `V69_M62_S3F2_OPERATOR_RULINGS_AND_EVAL_V2.md` | same range | the **human rulings H1–H6**, body-free review evidence (D27), eval-v2, D28 |
| **S3G** | COMPLETE | `V69_M62_S3G_QUALITY_TRAINING_CANDIDATE_DESIGN.md` | `28f1d45`…`a167420` | **the predeclared acceptance gates SV/QG/FG/OG** — §6 of that document; train-v1; D29, D30 |
| **S3G.1** | COMPLETE | `V69_M62_S3G1_PRETRAIN_QUALIFICATION.md` | same range | the verified cache and the real tokenizer audit (0 of 128 rows truncate at 512) |
| **S3G.2** | COMPLETE | `V69_M62_S3G2_VALIDATION_WIRING.md` | same range | **D31** — the validation wiring, the cadence, and the checkpoint-safety argument |
| **S3H** | SUCCESS | `V69_M62_S3H_FIRST_QUALITY_LIVE_TRAINING.md` | `a167420`…`4772a2c` | candidate 001's training run of record |
| **S3I.0** | COMPLETE | `V69_M62_S3I0_EVALUATION_RUNTIME_QUALIFICATION.md` | `4772a2c`…`6a3a7fa` | the load benchmark and `KEEP_EXISTING_LOADING_STRATEGY`; **D32, D33** |
| **S3I** | BLOCKED | `V69_M62_S3I_FIRST_QUALITY_HELDOUT_EVALUATION.md` | same range | the pre-token gate that stopped: B1 runtime absent, B2 → **D34** |
| **S3I.1** | COMPLETE | `V69_M62_S3I1_KALI_RUNTIME_AND_CANONICAL_LINEAGE.md` | same range | **D34 fixed** — canonical lineage declared, not discovered; the Kali evaluation runtime |
| **S3I LIVE** | PASS / `NOT_ELIGIBLE` | `V69_M62_S3I_LIVE_QUALITY_HELDOUT_EVALUATION.md` | same range | **candidate 001's run of record** — all nine vetoes pass, QG-2/QG-3/FG-1/FG-2 fail |
| **S3J** | PARTIAL | `V69_M62_S3J_SECOND_QUALITY_CANDIDATE_DESIGN.md` | `6a3a7fa`…`8381c64` | train-v2, eval-v3, candidate 002's configuration; **D35, D36** |
| **S3J.1** | PASS | `V69_M62_S3J1_KALI_TRAINING_RUNTIME_QUALIFICATION.md` | `8381c64`…`4ec4b36` | the separate training runtime and where each dependency version came from |
| **S3K** | SUCCESS | `V69_M62_S3K_SECOND_QUALITY_LIVE_TRAINING.md` | `4ec4b36`…`0827689` | candidate 002's training run of record |
| **S3L** | PASS / `NOT_ELIGIBLE` | `V69_M62_S3L_SECOND_QUALITY_HELDOUT_EVALUATION.md` | `0827689`…`22113a0` | **candidate 002's run of record** — three of nine security vetoes FAILED |
| **S3M** | PASS | `V69_M62_S3M_STRUCTURED_OUTPUT_DIAGNOSIS.md` | `22113a0`…`06480cb` | **why 7/9, twice** — one failure not two, and a termination failure; **D37, D38, D39** |
| **S3M.1** | PASS | `V69_M62_S3M1_D37_TEMPLATE_PARITY_QUALIFICATION.md` | `06480cb`…`475f3c9` | **D37 fixed** — train/eval render parity and `chat_render_policy_hash` |
| **S3M.2** | PASS | `V69_M62_S3M2_D38_OUTPUT_BUDGET_INSTRUMENTATION.md` | `475f3c9`…`4c669fa` | **D38 fixed, observability only** — and why there is no D38 gate |
| **S3N** | PASS | `V69_M62_S3N_FRESH_EVAL_V4_FREEZE.md` | `4c669fa`…`ec446e3` | **eval-v4 `FROZEN_UNUSED`**, the preregistration, and the holdout body firewall (§17) |
| **S3N.1** | PASS | `V69_M62_S3N1_CONTROL_PLANE_V2_ZERO_TRUST_MIGRATION.md` | `ec446e3`…`d8462f5` | **this architecture** — the archive, the snapshot chain, the verifier, the threat model |
| **S3O** | PASS | `V69_M62_S3O_CANDIDATE003_CONTROLLED_DESIGN.md` | `d8462f5`… | **candidate 003 `DESIGNED_UNTRAINED`** — the one-axis diff, the per-lineage id derivation, and the two verifier holes a rename and a missing state arm left open |
| **S3P** | PASS | `V69_M62_S3P_CANDIDATE003_LIVE_TRAINING.md` | `bac49c4`…`55e6eaa` | **candidate 003 `TRAINED_UNEVALUATED`** — one TRAIN capability created and consumed once, 40/40 steps, adapter `6ccd8fdc…`; the portable training receipt that lets a clone with no weights still establish the run; the first live training under D37. Nothing evaluated, `eval-v4` still `FROZEN_UNUSED` |
| **S3Q.0** | PASS | `V69_M62_S3Q0_EVAL_CEREMONY_QUALIFICATION.md` | `05c043b`… | **the evaluation ceremony, qualified before it runs** — three plan digests moved from PROXY to exact runtime identity; `performs_inference` corrected on an executable plan; the durable `holdout_model_facing_committed` event and the prospective spend rule; a terminal-ledger failure can no longer return clean success; the plan-ledger orphan directory; token-silent `--live-preflight`; the portable evaluation receipt and `EVALUATED_*` anti-circularity. **No model, no `eval-v4`, no authority** |
| **S3Q.0.1** | PASS | `V69_M62_S3Q01_EVAL_RECEIPT_HARDENING.md` | `b928f9d`…`c47fa1d` | **the portable evaluation evidence chain, closed before the irreversible act** — ten findings reproduced against `m62.eval_receipt.1` and fixed in `.2`: empty adapter identities, an authority event that does not exist, a standalone `--verify` that validated nothing, a caller-renamed candidate, an unchecked source commit, an unknown ledger event becoming the terminal witness, a set of plan hashes standing in for one, a verdict COPIED rather than evidenced, and a `first_task_id` binding the holdout firewall would have refused **after** the spend. `EVALUATED_*` is now REDERIVED by the production decision function from body-free evidence. **No model, no `eval-v4`, no authority** |
| **S3Q** | MEASURED · NOT ELIGIBLE | `V69_M62_S3Q_CANDIDATE003_LIVE_HELDOUT_EVALUATION.md` | `c2c025e`… | **the one authorised evaluation of candidate 003 against `eval-v4`** — one plan, one model-facing commit, one terminal event, **72 results, 0 generation errors**. Security clean for the first time in M62: 0 blockers, 0 new regressions, 3 improvements, the baseline's secret leak fixed. Task success 24/36 → 25/36, reward 0.5461 → 0.5903, and schema validity 9/9 → 8/9 — the one deterministic gate that blocked it. `eval-v4` is **`USED_IMMUTABLE`** and there is no rerun |
| **S3Q.0.2** | PASS | `V69_M62_S3Q02_SEAL_RECOVERY.md` | `98ff42a`…`7cc6d26` | **sealing a measurement that had already happened** — `m62.eval_receipt.2`, qualified against synthetic evidence, refused the real one three times (**D40** three-way verdict partition · **D41** ASCII-only vs `U+2212` · **D42** evaluation source conflated with seal source). A pre-repair measurement witness was committed ALONE at the evaluation source, the contract moved to `.3`, and the EXISTING measurement was sealed. **0 models loaded, 0 generations, 0 new evaluation attempts, no figure moved.** `.1` and `.2` keep their exact historical semantics |
| **S3U** | PASS | `V69_M62_S3U_CANDIDATE004_SINGLE_AXIS_DESIGN.md` | `43cb590`… | **candidate 004 `DESIGNED_UNTRAINED`, one dial** — an explicit human operator ruling let M62 continue and let candidate 004 be DESIGNED as a learning-rate experiment at `5e-5`, superseding **one clause of one** generation-8 `ruled_out` entry, prospectively and for this candidate only. Epoch, rank, alpha and dropout stay barred. The option is DERIVED from candidate 003's, so the measured semantic diff is exactly `{learning_rate}`; `verify_single_axis` refuses a second dial, a dial set back to the reference, and any rate the operator did not rule. The ruling is recorded body-free with the phrase withheld. **0 model loads, 0 generations, 0 optimizer steps, 0 eval attempts, 0 `eval-v5` semantic access. No adapter, no receipt, no authority of any kind** |

| **S3V** | PASS | `V69_M62_S3V_CANDIDATE004_LIVE_TRAINING.md` | `50d9de4`… | **candidate 004 `TRAINED_UNEVALUATED`, one run** — one plan-bound single-use human `TRAIN` authority, created once and consumed once, spent from `80565d3` on CPU/FP32 against `train-v2` unchanged: 40/40 optimizer steps, 2/2 epochs, 0 truncations, train loss 3.591112, two diagnostic validation losses. Adapter `a105e01c`… verifies at 392 LoRA tensors and 10,092,544 parameters. **Loss is not eligibility evidence** and eligibility stays UNKNOWN. Fixed a receipt builder that attributed every later candidate's design/training to S3O/S3P, and closed eight vacuous `check_training_receipt` bindings the empty-set early return had hidden since generation 3 (30/30 mutations now refused). **0 generations, 0 eval attempts, 0 `eval-v5` semantic access. `eval-v5` FROZEN_UNUSED, `spent_by` null. No EVAL authority, no promotion, no retry** |

| **S3X.0** | PASS | `V69_M62_S3X0_PRESPEND_HOLDOUT_FIREWALL_RECOVERY.md` | `dcd38cf`… | **a pre-authorisation holdout firewall breach, recovered; `eval-v5` RETIRED FROM ELIGIBILITY USE, unspent** — one `eval-v5` body reached an orchestration session through Python's representation machinery **before any `EVAL` authorisation existed**. The persistence firewall held; the missing one was the in-memory orchestration-display firewall, including `repr` of a **bound method**, which interpolates `repr(__self__)` — so displaying `pack.pack_hash` *without calling it* rendered every task body (**D44**, FIXED, **is_gate true**). `schemas.body_free_repr` guards **by type**, not by field list; 20 families of evaluation identity are byte-identical. An operator ruling then retired `eval-v5` from future eligibility — **prospectively, and without touching its lifecycle**: it stays `FROZEN_UNUSED` with `spent_by` null because **no model ever saw it** — and migrated the reviewed snapshot budget `32,768 → 34,816`. 514 residue files deleted body-blind; the incident transcript deliberately preserved. **0 weight loads, 0 generations, 0 eval attempts, 0 holdout spends, 0 `EVAL` authority. Candidate 004 stays `TRAINED_UNEVALUATED`; a fresh `eval-v6` is REQUIRED** |
| **S3X.1** | PASS | `V69_M62_S3X1_EVAL_V6_FREEZE.md` | `6667cf0`… | **a fresh `eval-v6` holdout authored, qualified and FROZEN UNSPENT** — the replacement the `eval-v5` ELIGIBILITY retirement requires. 36 tasks over the frozen contract cell for cell: splits 12/12/12, families 12/9/9/6, decision classes 12/6/18 **derived** by the pack builder rather than authored. Material written from the family contracts and split purposes alone; **no `v1`-`v5` prompt, target or task body was read to author it**. Freshness **measured, not asserted**: exact disjointness over six identity surfaces against all five prior versions, and the production near-duplicate comparator across the holdout/holdout boundary — **2,564 comparisons, 0 exact overlaps, 0 WARN, 0 BLOCK**, no ceiling. The 16-check leakage analyser is `clean` against both training corpora, **0 findings, 0 blocking**, with `semantic_similarity` still UNAVAILABLE. Lineage **DECLARED** onto `v5` under D34: retirement rules on what may be measured against, ancestry on where a corpus came from. Deterministic across independent roots. Two production gates refused the corpus first and both are recorded — the safe-boundary validator rejected one adversarial prompt, and the declared response schema rejected six tool targets. Capacity proved **before** authoring: **no budget raised**, generation 13 fits on lossless compaction with **30 carried-forward clauses checked fail-closed**. **0 weight loads, 0 generations, 0 eval attempts, 0 holdout spends, 0 `EVAL` authority. Candidate 004 stays `TRAINED_UNEVALUATED`, both evaluation fields null** |
| **S3Y** | MEASURED · ELIGIBLE FOR HUMAN REVIEW | `V69_M62_S3Y_CANDIDATE004_LIVE_HELDOUT_EVALUATION.md` | `1fd8a2a`… | **the one authorised evaluation of candidate 004 against `eval-v6`** — one plan, one human `EVAL` authority consumed and not reusable, one model-facing commit, one terminal event, **36/36 pairs measured, 0 missing, 0 generation errors**. Every deterministic gate passed — security, coverage, family, quality, statistical, operational, `blocking_count` 0 — so candidate 004 is **`EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW`**, which REQUESTS a human decision and is not one. **Read the counts before the verdict: 14 regressed against 9 improved**, 10 unchanged, 3 security improvements and **0 security regressions**; paired mean delta **+0.0751** with CI95 **[−0.0040, +0.1793]**, which does **not** exclude a regression (`regression_not_excluded`). The candidate cleared the preregistered bar and did **not** demonstrate a quality improvement; whether 1e-4 → 5e-5 helped is **not** established. `eval-v6` is **`USED_IMMUTABLE`** and there is no rerun; `eval-v5` stays FROZEN_UNUSED, unspent, retired. Capacity was proved conservative BEFORE the spend (S3Y.CAP1 — the gate had been passing on a `test_baseline` stand-in four bytes narrower than any truthful one, which put the true worst case at 1023 against a 1024 floor); generation 14 lands at **33,677 bytes, 1,139 headroom**, no budget raised. **No promotion authority exists or was requested; nothing was promoted, merged, tagged or released** |
| **S3Z** | HELD — GOVERNANCE ONLY | `V69_M62_S3Z_CANDIDATE004_HOLD_DECISION.md` | `9953b07`… | **the explicit human decision on candidate 004's ELIGIBLE measurement: HOLD** — accept the S3Y evidence as valid, preserve candidate 004 unchanged, promote nothing. **HOLD is a GOVERNANCE decision, not a candidate state**: `CANDIDATE_STATES`, `CANDIDATE_TRANSITIONS` and every guard are untouched, no `HOLD`/`HELD`/`DEFERRED` state was invented, and candidate 004 stays **`EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW`** — the only legal move out of it remains `-> PROMOTED` under a `HUMAN_PROMOTION_AUTHORITY` that does not exist. Recorded only after **two body-blind read-only audits both PASSED**: the immutable evidence chain re-derived from Git and the portable receipt (canonical verification, **0 problems**; 1 attempt, 1 spend, 1 plan consumption; 36/36 pairs, 0 missing; `retry_authorized` false), and a **post-hoc-weakening audit** of every test/control-plane change made after the result was known — 17 files, each mapping to a sealed-generation pin, superseded absence assertion, non-vacuity preservation, strengthening or fixture isolation, with **0** threshold, gate, grader, policy, receipt or transition weakenings, **0** invariants dropped, **0** hidden failures, **0** vacuous mutations. **Why HOLD and not PROMOTE:** eligibility passed but general superiority was not demonstrated and regression is not excluded. **Why not REJECT:** the measurement is valid and security regressions were zero. `eval-v6` stays `USED_IMMUTABLE` with **no second look**; `eval-v5` stays FROZEN_UNUSED, `spent_by` null, RETIRED. Generation 15 lands at **33,742 bytes, 1,074 headroom**, no budget raised, every scientific section byte-identical to generation 14. **Candidate 005, `eval-v7`, further training and further evaluation are all NOT authorised; the loop is PAUSED pending a separate operator roadmap ruling** |

| **S4B** | PREREGISTERED — DESIGN ONLY | `V69_M63_S4B_CANDIDATE005_SINGLE_AXIS_DESIGN.md` | `9e50a74`… | **candidate 005 `DESIGNED_UNTRAINED` under a SECOND human operator ruling, `S4B-001`: `learning_rate` 5e-5 -> 2.5e-5, candidate 005 only, prospectively, only to that value.** Nothing trained, evaluated or promoted; **0** weight loads, generations, eval attempts and holdout spends. The option is DERIVED from candidate 004's by expansion, so the measured semantic diff is exactly `{learning_rate}` and `verify_single_axis` refuses a second dial, an unmoved axis or an unruled value. Corpus **`train-v2` unchanged** — 182 records, reference hash byte-equal to candidate 004's sealed receipt. Candidate 004 comes out exactly as it went in: `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW`, **HOLD**, not promoted. **Two runtime defects found and fixed under their own commits:** `core/runtime_doctor.py` imported `loguru` at module scope and so could not be imported in the bare environment it exists to diagnose; and `_parent_snapshot` read a V3 parent RAW, so generation 17 — the first with a V3 parent — would have had its transition table silently reduced to the fresh-ordinal rule. Both failed closed. The training runtime was rebuilt as `PY314_NATIVE` at the exact reviewed pins after cp314 availability was checked for every one; the broken historical venv was measured and **not mutated**. Generation 17 lands at **6,846 bytes, 27,970 headroom**; the projected `TRAINED_UNEVALUATED` terminal state at **7,315 / 27,501**, proved before authority was asked for. **`eval-v7`, evaluation, promotion, a candidate 006 and a second run are all NOT authorised** |

> **Gap, recorded rather than papered over.** **S3R**, **S3S**, **S3S.1**, **S3T.0** and **S3W.0** each have a milestone document in `jarvis/docs/` and no row above; S3U did not write rows for milestones it did not run. Find them by filename, or through the generation they wrote in `state/m62/snapshots/`.

---

## Defects

Current state for the defects that still bind operation is in `PROGRESS.md` §7. This table
routes to the *history* of every one, including those long closed.

| Defect | Where it was found | Where it was closed | Archive section |
|---|---|---|---|
| D1–D10 | S1 – S3E.1 | closed in place | §13 |
| D11–D15 | S3D | S3D | §13 |
| D16 | S3D.1 | S3D.1 — `checkpoint_strategy` refused at config validation | §13 |
| D17–D23 | S3E – S3E.2 | closed in place | §13 |
| D24 | S3F — grader saturation | S3F | §13, §14.2 |
| D25 | S3F — report serialisation state | S3F | §13, §14.6 |
| D26a / D26b | S3F.1 — thinking policy, real schema validation | S3F.1 | §13, §14.7 |
| D27 | S3F.2 — notes quote the response | S3F.2 | §13 |
| **D28** | S3F.2 — no tool-call transport | **OPEN** | §13, §14.15 |
| **D29** | S3G — refusal detector phrasing | **accepted limitation** | §13, §14.21 |
| D30 | S3G — cache blocker dropped | S3G | §13, §14.28 |
| D31 | S3G.1 — VALIDATION reached nothing | S3G.2 | §13, §14.31 |
| D32 | S3I.0 — eval-v2 digest | superseded by D34 | §13, §14.45 |
| **D33** | S3I.0 — timeout not enforced | **OPEN** | §13, §14.44 |
| D34 | S3I — lineage-dependent digest | S3I.1 | §13, §14.46 |
| D35 | S3J — operator ruling on spent holdouts | ruling stands | §2 (S3J block), §14.67 |
| D36 | S3J — host-dependent dataset identity | S3J | §13, §14.56 |
| D37 | S3M — train/eval render divergence | S3M.1 | §13, §14.79 |
| D38 | S3M — output budget has no metric | S3M.2 (observability only) | §13, §14.80 |
| **D39** | S3M — order-dependent test isolation | **OPEN** | §13, §14.81 |

---

## Where the long-form material went

| What you might be looking for | Archive section |
|---|---|
| The full per-milestone status matrices (S3E.2 → S3N) | §2 |
| The non-negotiable safety and repository invariants, as originally written | §3 |
| The milestone-by-milestone timeline and the 52-commit index | §4 |
| The run-004 smoke adapter and its 40 structural checks | §6 |
| The held-out corpus history and the v1/v2/v3/v4 identity table | §7 |
| The task-pack builder's enforced properties, and the two-digests distinction | §8 |
| S3E.2's plan, generations, empirical results and security findings | §9, §10, §11 |
| The per-request model-load cost measurement | §12 |
| The complete D1–D39 defect ledger with fixes, commits and regression tests | §13 |
| All 94 recorded limitations, including the struck-through superseded ones | §14 |
| Every historical test baseline and gate result, per milestone and per interpreter | §15 |
| The full Git checkpoint table | §16 |
| The runtime artifact policy in full | §17 |
| Every "what future sessions must NOT redo" rule, per milestone | §18 |
| The complete NEXT history, including all superseded framings | §19 |
| The old fast-start read order (superseded by the bootstrap contract in `PROGRESS.md` §11) | §20 |
| The old update protocol (superseded by `PROGRESS.md` §15) | §21 |

---

## Conventions that changed at S3N.1

| Before | Now | Note |
|---|---|---|
| `PROGRESS.md` held current state *and* history | `PROGRESS.md` is current state only | history is the archive |
| A holdout awaiting use was `FROZEN_UNSEEN` (S3J/S3K) | `FROZEN_UNUSED` | same semantics, one canonical spelling; the old one survives only in the archive |
| Milestone reports were appended to `PROGRESS.md` | a milestone document plus one index row | see `PROGRESS.md` §15 |
| State was prose | prose plus a verified snapshot | see `PROGRESS.md` §0 |
